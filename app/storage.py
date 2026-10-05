"""SQLite-backed local storage with optimistic concurrency control.

Each resource row carries a monotonically increasing ``version``. Updates use
a compare-and-swap: ``UPDATE ... WHERE patient_id=? AND version=?``. If no row
matches we distinguish the two causes:
  - resource absent            -> NotFoundError (NOT_FOUND)
  - row exists, version stale  -> ConcurrencyError (VERSION_CONFLICT)
A single writer thread is enforced with a re-entrant lock plus SQLite's own
transaction, so concurrent updates cannot lose writes or double-apply.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import asdict

log = logging.getLogger(__name__)

from app.errors import (
    ConcurrencyError,
    ErrorCode,
    IngestionError,
    NotFoundError,
    VersionNotFoundError,
)
from app.models import (
    Coding,
    ObservationRecord,
    PatientResource,
    StoredResource,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS resources (
    patient_id TEXT PRIMARY KEY,
    version    INTEGER NOT NULL,
    updated_at REAL NOT NULL,
    body       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS resource_history (
    patient_id TEXT NOT NULL,
    version    INTEGER NOT NULL,
    updated_at REAL NOT NULL,
    body       TEXT NOT NULL,
    PRIMARY KEY (patient_id, version)
);
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key         TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    response    TEXT NOT NULL,
    created_at  REAL NOT NULL
);
"""


def _serialize(resource: PatientResource) -> str:
    return json.dumps(asdict(resource), sort_keys=True)


def _deserialize(patient_id: str, version: int, updated_at: float, body: str) -> StoredResource:
    raw = json.loads(body)
    observations = [
        ObservationRecord(code=Coding(**o["code"]), value=o["value"],
                          unit=o.get("unit"), effective=o.get("effective"))
        for o in raw.get("observations", [])
    ]
    return StoredResource(
        resource=PatientResource(
            patient_id=raw["patient_id"],
            active=raw.get("active", True),
            identifiers=dict(raw.get("identifiers", {})),
            observations=observations,
        ),
        version=version,
        updated_at=updated_at,
    )


class ResourceStore:
    def __init__(self, db_path: str = ":memory:") -> None:
        # check_same_thread=False + our own lock: safe for concurrent test threads.
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._lock = threading.RLock()
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)
            self._migrate_legacy_history()

    def _migrate_legacy_history(self) -> None:
        """Backfill ``resource_history`` for databases written by versions of
        this service that predate the history table (current state only).

        For every patient with no history rows at all we know how many writes
        succeeded (the current ``version``) but only the *latest* body and
        timestamp were retained. We therefore synthesize rows for versions
        1..current: the current version gets the true body and ``updated_at``;
        earlier versions are best-effort reconstructions carrying the
        last-known body and the same timestamp. After the migration every
        prior write is listed in order and every version number is
        retrievable; new writes append real snapshots as usual. The migration
        is idempotent (skips patients that already have history) and runs in
        the same transaction as schema creation, so a crash mid-migration
        simply re-runs it on next open.
        """
        rows = self._conn.execute(
            "SELECT r.patient_id, r.version, r.updated_at, r.body"
            " FROM resources r"
            " WHERE NOT EXISTS (SELECT 1 FROM resource_history h"
            "                   WHERE h.patient_id = r.patient_id)"
        ).fetchall()
        for patient_id, version, updated_at, body in rows:
            for v in range(1, version + 1):
                self._conn.execute(
                    "INSERT INTO resource_history"
                    " (patient_id, version, updated_at, body) VALUES (?,?,?,?)",
                    (patient_id, v, updated_at, body),
                )
            log.info(
                "legacy migration: patient=%s backfilled %d version(s)"
                " (1..%d); versions < %d carry last-known content",
                patient_id, version, version, version,
            )

    def get(self, patient_id: str) -> StoredResource:
        """Return the stored resource or raise NotFoundError."""
        with self._lock:
            row = self._conn.execute(
                "SELECT version, updated_at, body FROM resources WHERE patient_id=?",
                (patient_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError(
                ErrorCode.NOT_FOUND,
                f"patient '{patient_id}' does not exist",
                detail={"patient_id": patient_id},
            )
        return _deserialize(patient_id, row[0], row[1], row[2])

    def create_tx(self, resource: PatientResource) -> StoredResource:
        """Transaction-participating create: writes current row + first
        history snapshot but does NOT commit. Caller must hold ``self._lock``
        and be inside ``with self._conn`` so the commit can be grouped with
        other writes (e.g. the idempotency-key record)."""
        now = time.time()
        body = _serialize(resource)
        try:
            self._conn.execute(
                "INSERT INTO resources (patient_id, version, updated_at, body)"
                " VALUES (?, 1, ?, ?)",
                (resource.patient_id, now, body),
            )
            self._conn.execute(
                "INSERT INTO resource_history"
                " (patient_id, version, updated_at, body) VALUES (?, 1, ?, ?)",
                (resource.patient_id, now, body),
            )
        except sqlite3.IntegrityError:
            raise ConcurrencyError(
                ErrorCode.VERSION_CONFLICT,
                f"patient '{resource.patient_id}' already exists",
                detail={"patient_id": resource.patient_id},
            ) from None
        return StoredResource(resource=resource, version=1, updated_at=now)

    def create(self, resource: PatientResource) -> StoredResource:
        """Insert a new resource at version 1; duplicate ids are rejected.

        The current row and its first history snapshot are written in one
        transaction, so current state and history can never diverge and both
        survive a restart (same SQLite file).
        """
        with self._lock, self._conn:
            return self.create_tx(resource)

    def update_tx(self, resource: PatientResource, expected_version: int) -> StoredResource:
        """Transaction-participating compare-and-swap update; does NOT commit
        (same contract as :meth:`create_tx`)."""
        now = time.time()
        body = _serialize(resource)
        cursor = self._conn.execute(
            "UPDATE resources SET version=version+1, updated_at=?, body=?"
            " WHERE patient_id=? AND version=?",
            (now, body, resource.patient_id, expected_version),
        )
        if cursor.rowcount == 0:
            exists = self._conn.execute(
                "SELECT version FROM resources WHERE patient_id=?",
                (resource.patient_id,),
            ).fetchone()
            if exists is None:
                raise NotFoundError(
                    ErrorCode.NOT_FOUND,
                    f"patient '{resource.patient_id}' does not exist",
                    detail={"patient_id": resource.patient_id},
                )
            raise ConcurrencyError(
                ErrorCode.VERSION_CONFLICT,
                f"stale version {expected_version}; current is {exists[0]}",
                detail={"patient_id": resource.patient_id,
                        "expected_version": expected_version,
                        "current_version": exists[0]},
            )
        self._conn.execute(
            "INSERT INTO resource_history"
            " (patient_id, version, updated_at, body) VALUES (?, ?, ?, ?)",
            (resource.patient_id, expected_version + 1, now, body),
        )
        return StoredResource(resource=resource, version=expected_version + 1,
                              updated_at=now)

    def update(self, resource: PatientResource, expected_version: int) -> StoredResource:
        """Compare-and-swap update; stale versions raise ConcurrencyError.

        On success the new snapshot is appended to resource_history in the
        same transaction as the current-row swap.
        """
        with self._lock, self._conn:
            return self.update_tx(resource, expected_version)

    def get_version(self, patient_id: str, version: int) -> StoredResource:
        """Return the immutable snapshot at ``version``.

        Distinguishes NOT_FOUND (patient unknown) from VERSION_NOT_FOUND
        (patient exists, version never committed).
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT updated_at, body FROM resource_history"
                " WHERE patient_id=? AND version=?",
                (patient_id, version),
            ).fetchone()
            if row is None:
                exists = self._conn.execute(
                    "SELECT 1 FROM resources WHERE patient_id=?", (patient_id,),
                ).fetchone()
                if exists is None:
                    raise NotFoundError(
                        ErrorCode.NOT_FOUND,
                        f"patient '{patient_id}' does not exist",
                        detail={"patient_id": patient_id},
                    )
                raise VersionNotFoundError(
                    ErrorCode.VERSION_NOT_FOUND,
                    f"patient '{patient_id}' has no version {version}",
                    detail={"patient_id": patient_id, "version": version},
                )
        return _deserialize(patient_id, version, row[0], row[1])

    def list_history(self, patient_id: str) -> list[StoredResource]:
        """All committed versions of a patient, oldest first."""
        with self._lock:
            exists = self._conn.execute(
                "SELECT 1 FROM resources WHERE patient_id=?", (patient_id,),
            ).fetchone()
            if exists is None:
                raise NotFoundError(
                    ErrorCode.NOT_FOUND,
                    f"patient '{patient_id}' does not exist",
                    detail={"patient_id": patient_id},
                )
            rows = self._conn.execute(
                "SELECT version, updated_at, body FROM resource_history"
                " WHERE patient_id=? ORDER BY version",
                (patient_id,),
            ).fetchall()
        return [_deserialize(patient_id, v, ts, body) for v, ts, body in rows]

    def apply_idempotent(self, key: str, fingerprint: str, fn) -> tuple[dict, bool]:
        """Run ``fn`` (a create_tx/update_tx) exactly once per idempotency key.

        Returns ``(response, replayed)``. If the key was already committed,
        the recorded response is returned without invoking ``fn`` — a retried
        submission never creates an extra version. Reusing the same key with
        a *different* request fingerprint is rejected as MALFORMED_PAYLOAD so
        the two cases stay distinguishable.

        Atomicity: the key lookup, the resource write and the key record are
        committed in ONE transaction under one lock. A crash or restart
        therefore cannot leave the write committed without its key (which
        would make the client's retry fail with VERSION_CONFLICT or write a
        second time): either both are durable and the retry cleanly replays
        the recorded response, or neither is and the retry re-executes the
        submission from scratch. Concurrent retries of the same key are
        serialized by the lock, so exactly one of them performs the write.
        ``fn`` must be a transaction-participating callable (create_tx /
        update_tx) so it joins this transaction instead of committing early.
        """
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT fingerprint, response FROM idempotency_keys WHERE key=?",
                (key,),
            ).fetchone()
            if row is not None:
                if row[0] != fingerprint:
                    raise IngestionError(
                        ErrorCode.MALFORMED_PAYLOAD,
                        f"idempotency key '{key}' was already used with a"
                        " different request",
                        detail={"idempotency_key": key},
                    )
                return json.loads(row[1]), True
            stored: StoredResource = fn()  # joins this transaction, no commit
            response = {"patient_id": stored.resource.patient_id,
                        "version": stored.version}
            self._conn.execute(
                "INSERT INTO idempotency_keys (key, fingerprint, response,"
                " created_at) VALUES (?,?,?,?)",
                (key, fingerprint, json.dumps(response, sort_keys=True),
                 time.time()),
            )
            return response, False

    def close(self) -> None:
        with self._lock:
            self._conn.close()
