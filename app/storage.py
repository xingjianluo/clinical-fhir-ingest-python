"""SQLite-backed local storage with optimistic concurrency control,
append-only version history, and an idempotency ledger.

Durability / restart semantics:
  - All state lives in one SQLite file (WAL mode, FULL synchronous): the
    current-state table, the append-only history table and the idempotency
    ledger. Committed writes survive process restarts; on reopen the schema
    is created if missing and pre-history databases are backfilled so the
    version chain never breaks across an upgrade.

Concurrency:
  - Each resource row carries a monotonically increasing ``version``. Updates
    use a compare-and-swap: ``UPDATE ... WHERE patient_id=? AND version=?``.
    If no row matches we distinguish the two causes:
      - resource absent            -> NotFoundError (NOT_FOUND)
      - row exists, version stale  -> ConcurrencyError (VERSION_CONFLICT)
  - A single writer is enforced with a re-entrant lock plus SQLite's own
    transaction, so concurrent updates cannot lose writes or double-apply.

Idempotency:
  - Mutations may carry a caller-supplied ``request_id``. The mutation and
    its ledger entry commit in the SAME transaction, so a retry after a
    timeout/network failure replays the recorded outcome instead of
    applying twice: no extra version, no extra history row.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import asdict

from app.errors import ConcurrencyError, ErrorCode, NotFoundError
from app.models import (
    Coding,
    ObservationRecord,
    PatientResource,
    StoredResource,
    VersionEntry,
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
    request_id TEXT,
    PRIMARY KEY (patient_id, version)
);
CREATE TABLE IF NOT EXISTS requests (
    request_id TEXT PRIMARY KEY,
    patient_id TEXT NOT NULL,
    action     TEXT NOT NULL,
    version    INTEGER NOT NULL,
    updated_at REAL NOT NULL
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
            # Backfill history for databases written before history existed,
            # so the version chain stays complete across upgrades/restarts.
            self._conn.execute(
                "INSERT OR IGNORE INTO resource_history"
                " (patient_id, version, updated_at, body, request_id)"
                " SELECT patient_id, version, updated_at, body, NULL"
                " FROM resources"
            )

    # -- idempotency -------------------------------------------------------
    def _replay(self, request_id: str) -> StoredResource | None:
        """Return the recorded outcome of a previous submission, if any."""
        row = self._conn.execute(
            "SELECT patient_id, version, updated_at FROM requests"
            " WHERE request_id=?",
            (request_id,),
        ).fetchone()
        if row is None:
            return None
        patient_id, version, updated_at = row
        hist = self._conn.execute(
            "SELECT body FROM resource_history WHERE patient_id=? AND version=?",
            (patient_id, version),
        ).fetchone()
        stored = _deserialize(patient_id, version, updated_at, hist[0])
        stored.replayed = True
        return stored

    def _record_request(self, request_id: str | None, patient_id: str,
                        action: str, version: int, updated_at: float) -> None:
        if request_id is not None:
            self._conn.execute(
                "INSERT INTO requests (request_id, patient_id, action, version,"
                " updated_at) VALUES (?,?,?,?,?)",
                (request_id, patient_id, action, version, updated_at),
            )

    # -- reads ---------------------------------------------------------------
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

    def get_version(self, patient_id: str, version: int) -> StoredResource:
        """Return the snapshot at ``version``.

        Distinct failures: patient unknown -> NOT_FOUND; patient known but
        version missing -> VERSION_NOT_FOUND (with the valid range).
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT updated_at, body FROM resource_history"
                " WHERE patient_id=? AND version=?",
                (patient_id, version),
            ).fetchone()
            if row is None:
                bounds = self._conn.execute(
                    "SELECT MIN(version), MAX(version) FROM resource_history"
                    " WHERE patient_id=?",
                    (patient_id,),
                ).fetchone()
                if bounds[0] is None:
                    raise NotFoundError(
                        ErrorCode.NOT_FOUND,
                        f"patient '{patient_id}' does not exist",
                        detail={"patient_id": patient_id},
                    )
                raise NotFoundError(
                    ErrorCode.VERSION_NOT_FOUND,
                    f"patient '{patient_id}' has no version {version};"
                    f" valid range is {bounds[0]}..{bounds[1]}",
                    detail={"patient_id": patient_id, "version": version,
                            "min_version": bounds[0], "max_version": bounds[1]},
                )
        return _deserialize(patient_id, version, row[0], row[1])

    def list_history(self, patient_id: str) -> list[VersionEntry]:
        """Return every stored version in ascending order."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT version, updated_at FROM resource_history"
                " WHERE patient_id=? ORDER BY version",
                (patient_id,),
            ).fetchall()
        if not rows:
            raise NotFoundError(
                ErrorCode.NOT_FOUND,
                f"patient '{patient_id}' does not exist",
                detail={"patient_id": patient_id},
            )
        return [VersionEntry(version=r[0], updated_at=r[1]) for r in rows]

    # -- writes --------------------------------------------------------------
    def create(self, resource: PatientResource,
               request_id: str | None = None) -> StoredResource:
        """Insert a new resource at version 1; duplicate ids are rejected.

        With ``request_id``, a retried submission replays the recorded
        outcome instead of raising or re-applying.
        """
        now = time.time()
        with self._lock, self._conn:
            if request_id is not None:
                prior = self._replay(request_id)
                if prior is not None:
                    return prior
            try:
                self._conn.execute(
                    "INSERT INTO resources (patient_id, version, updated_at, body)"
                    " VALUES (?, 1, ?, ?)",
                    (resource.patient_id, now, _serialize(resource)),
                )
            except sqlite3.IntegrityError:
                raise ConcurrencyError(
                    ErrorCode.VERSION_CONFLICT,
                    f"patient '{resource.patient_id}' already exists",
                    detail={"patient_id": resource.patient_id},
                ) from None
            self._conn.execute(
                "INSERT INTO resource_history"
                " (patient_id, version, updated_at, body, request_id)"
                " VALUES (?, 1, ?, ?, ?)",
                (resource.patient_id, now, _serialize(resource), request_id),
            )
            self._record_request(request_id, resource.patient_id, "create",
                                 1, now)
        return StoredResource(resource=resource, version=1, updated_at=now)

    def update(self, resource: PatientResource, expected_version: int,
               request_id: str | None = None) -> StoredResource:
        """Compare-and-swap update; stale versions raise ConcurrencyError.

        With ``request_id``, a retried submission replays the recorded
        outcome (same version, same body) instead of applying twice.
        """
        now = time.time()
        with self._lock, self._conn:
            if request_id is not None:
                prior = self._replay(request_id)
                if prior is not None:
                    return prior
            cursor = self._conn.execute(
                "UPDATE resources SET version=version+1, updated_at=?, body=?"
                " WHERE patient_id=? AND version=?",
                (now, _serialize(resource), resource.patient_id, expected_version),
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
            new_version = expected_version + 1
            self._conn.execute(
                "INSERT INTO resource_history"
                " (patient_id, version, updated_at, body, request_id)"
                " VALUES (?, ?, ?, ?, ?)",
                (resource.patient_id, new_version, now, _serialize(resource),
                 request_id),
            )
            self._record_request(request_id, resource.patient_id, "update",
                                 new_version, now)
        return StoredResource(resource=resource, version=new_version,
                              updated_at=now)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
