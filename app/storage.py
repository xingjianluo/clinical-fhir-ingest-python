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
import sqlite3
import threading
import time
from dataclasses import asdict

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

# Upgrade migration for databases written by builds that predate the
# resource_history table: those files only carry each patient's *current*
# row. Backfill a history snapshot for the current version of every resource
# that has none, so history is never empty and the current version is always
# retrievable by number. Earlier intermediate versions were never recorded
# by the old build and cannot be fabricated — reads of those versions keep
# raising VERSION_NOT_FOUND with the requested version in the detail.
# INSERT OR IGNORE makes the migration idempotent: reopening a migrated or
# fresh database is a no-op.
_MIGRATE_BACKFILL_HISTORY = """
INSERT OR IGNORE INTO resource_history (patient_id, version, updated_at, body)
SELECT patient_id, version, updated_at, body FROM resources;
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
            self._conn.execute(_MIGRATE_BACKFILL_HISTORY)

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

    def create(self, resource: PatientResource, *, commit: bool = True) -> StoredResource:
        """Insert a new resource at version 1; duplicate ids are rejected.

        The current row and its first history snapshot are written in one
        transaction, so current state and history can never diverge and both
        survive a restart (same SQLite file). ``commit=False`` skips the
        transaction wrapper so callers (``apply_idempotent``) can bundle the
        write with more statements in a single atomic commit.
        """
        with self._lock:
            if commit:
                with self._conn:
                    return self._create_locked(resource)
            return self._create_locked(resource)

    def _create_locked(self, resource: PatientResource) -> StoredResource:
        """``create`` with the lock already held and no commit of its own."""
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

    def update(self, resource: PatientResource, expected_version: int,
               *, commit: bool = True) -> StoredResource:
        """Compare-and-swap update; stale versions raise ConcurrencyError.

        On success the new snapshot is appended to resource_history in the
        same transaction as the current-row swap. ``commit=False`` skips the
        transaction wrapper so callers (``apply_idempotent``) can bundle the
        write with more statements in a single atomic commit.
        """
        with self._lock:
            if commit:
                with self._conn:
                    return self._update_locked(resource, expected_version)
            return self._update_locked(resource, expected_version)

    def _update_locked(self, resource: PatientResource,
                       expected_version: int) -> StoredResource:
        """``update`` with the lock already held and no commit of its own."""
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

    def _record_key(self, key: str, fingerprint: str, response: dict) -> None:
        """Insert the idempotency record; runs inside the caller's
        transaction so it commits (or rolls back) together with the write."""
        self._conn.execute(
            "INSERT INTO idempotency_keys (key, fingerprint, response,"
            " created_at) VALUES (?,?,?,?)",
            (key, fingerprint, json.dumps(response, sort_keys=True),
             time.time()),
        )

    def _lookup_key(self, key: str, fingerprint: str) -> dict | None:
        """Recorded response for ``key``, or None if never committed.

        A key reused with a *different* request fingerprint is rejected as
        MALFORMED_PAYLOAD so the two cases stay distinguishable.
        """
        row = self._conn.execute(
            "SELECT fingerprint, response FROM idempotency_keys WHERE key=?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        if row[0] != fingerprint:
            raise IngestionError(
                ErrorCode.MALFORMED_PAYLOAD,
                f"idempotency key '{key}' was already used with a"
                " different request",
                detail={"idempotency_key": key},
            )
        return json.loads(row[1])

    def apply_idempotent(self, key: str, fingerprint: str, fn) -> tuple[dict, bool]:
        """Run ``fn`` (a non-committing create/update) exactly once per key.

        Returns ``(response, replayed)``. If the key was already committed,
        the recorded response is returned without invoking ``fn`` — a retried
        submission never creates an extra version, even if the retry arrives
        after a restart (the record lives in the same SQLite file).

        The write and the idempotency record commit in ONE transaction:
        a crash or restart anywhere in the middle leaves either both or
        neither behind, so a later retry either replays the recorded
        response or performs the write cleanly — never errors out with a
        half-applied state, never writes twice. The lookup, write and key
        record happen under one lock, so concurrent retries of the same key
        are serialized and exactly one of them performs the write.
        """
        with self._lock:
            recorded = self._lookup_key(key, fingerprint)
            if recorded is not None:
                return recorded, True
            try:
                with self._conn:
                    stored = fn()  # create/update with commit=False
                    response = {"patient_id": stored.resource.patient_id,
                                "version": stored.version}
                    self._record_key(key, fingerprint, response)
            except sqlite3.IntegrityError:
                # Another process committed the same key between our lookup
                # and our insert; the transaction rolled our write back, so
                # the recorded response is the single source of truth.
                recorded = self._lookup_key(key, fingerprint)
                if recorded is not None:
                    return recorded, True
                raise
            return response, False

    def close(self) -> None:
        with self._lock:
            self._conn.close()
