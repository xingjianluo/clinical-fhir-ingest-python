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

from app.errors import ConcurrencyError, ErrorCode, NotFoundError
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

    def create(self, resource: PatientResource) -> StoredResource:
        """Insert a new resource at version 1; duplicate ids are rejected."""
        now = time.time()
        with self._lock, self._conn:
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
        return StoredResource(resource=resource, version=1, updated_at=now)

    def update(self, resource: PatientResource, expected_version: int) -> StoredResource:
        """Compare-and-swap update; stale versions raise ConcurrencyError."""
        now = time.time()
        with self._lock, self._conn:
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
        return StoredResource(resource=resource, version=expected_version + 1,
                              updated_at=now)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
