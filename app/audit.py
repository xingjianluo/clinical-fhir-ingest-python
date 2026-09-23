"""Append-only audit trail stored locally.

Audit records are written for every operation attempt (success and failure).
They are deliberately NOT exposed through any read/export endpoint; the only
accessor is ``list_all`` for forensic/administrative use outside the API.
"""
from __future__ import annotations

import sqlite3
import threading

from app.models import AuditRecord

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
    audit_id   TEXT PRIMARY KEY,
    timestamp  REAL NOT NULL,
    actor      TEXT NOT NULL,
    role       TEXT NOT NULL,
    action     TEXT NOT NULL,
    patient_id TEXT,
    outcome    TEXT NOT NULL,
    detail     TEXT NOT NULL
);
"""


class AuditLog:
    def __init__(self, db_path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._lock = threading.RLock()
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)

    def record(self, entry: AuditRecord) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO audit (audit_id, timestamp, actor, role, action,"
                " patient_id, outcome, detail) VALUES (?,?,?,?,?,?,?,?)",
                (entry.audit_id, entry.timestamp, entry.actor, entry.role,
                 entry.action, entry.patient_id, entry.outcome, entry.detail),
            )

    def list_all(self) -> list[AuditRecord]:
        """Internal/forensic accessor only; never wired to a read endpoint."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT audit_id, timestamp, actor, role, action, patient_id,"
                " outcome, detail FROM audit ORDER BY timestamp, audit_id"
            ).fetchall()
        return [AuditRecord(*row) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
