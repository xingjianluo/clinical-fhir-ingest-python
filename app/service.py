"""Orchestration: authorize -> normalize -> store -> audit.

Every attempt (success or failure) is appended to the audit trail, EXCEPT
idempotent replays: a retried submission (same ``request_id``) returns the
originally recorded outcome without applying again and without writing a
duplicate audit record. Errors propagate as ServiceError subclasses so
callers can distinguish causes.
"""
from __future__ import annotations

import os

from app import access, normalize
from app.audit import AuditLog
from app.errors import ServiceError
from app.models import AuditRecord, Role
from app.storage import ResourceStore


class ClinicalService:
    def __init__(self, db_path: str = ":memory:") -> None:
        if db_path == ":memory:":
            self._store = ResourceStore(":memory:")
            self._audit = AuditLog(":memory:")
        else:
            os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
            self._store = ResourceStore(db_path)
            self._audit = AuditLog(db_path)

    # -- internal helpers -------------------------------------------------
    def _record_audit(self, actor: str, role: Role, action: str,
               patient_id: str | None, outcome: str, detail: str) -> None:
        self._audit.record(AuditRecord.new(
            actor=actor, role=role.value, action=action,
            patient_id=patient_id, outcome=outcome, detail=detail,
        ))

    def _run(self, actor: str, role: Role, action: str,
             patient_id: str | None, fn):
        """Run ``fn`` (returning ``(result, replayed)``) with audit."""
        try:
            result, replayed = fn()
        except ServiceError as e:
            self._record_audit(actor, role, action, patient_id, "denied",
                        f"{e.code.value}: {e.message}")
            raise
        if not replayed:
            self._record_audit(actor, role, action, patient_id, "success", "ok")
        return result

    # -- public operations --------------------------------------------------
    def ingest(self, payload: dict, source_version: str, *,
               actor: str, role: Role,
               request_id: str | None = None) -> dict:
        def op():
            access.check_write(role)
            resource = normalize.normalize(payload, source_version)
            stored = self._store.create(resource, request_id=request_id)
            return {"patient_id": stored.resource.patient_id,
                    "version": stored.version}, stored.replayed
        return self._run(actor, role, "ingest",
                         payload.get("patient_id") if isinstance(payload, dict) else None,
                         op)

    def update(self, patient_id: str, payload: dict, source_version: str,
               expected_version: int, *, actor: str, role: Role,
               request_id: str | None = None) -> dict:
        def op():
            access.check_write(role)
            resource = normalize.normalize(payload, source_version)
            resource.patient_id = patient_id
            stored = self._store.update(resource, expected_version,
                                        request_id=request_id)
            return {"patient_id": stored.resource.patient_id,
                    "version": stored.version}, stored.replayed
        return self._run(actor, role, "update", patient_id, op)

    def read(self, patient_id: str, *, actor: str, role: Role) -> dict:
        def op():
            access.check_read(role, identified=True)
            stored = self._store.get(patient_id)
            view = access.identified_view(stored.resource)
            view["version"] = stored.version
            return view, False
        return self._run(actor, role, "read", patient_id, op)

    def export(self, patient_id: str, *, actor: str, role: Role) -> dict:
        def op():
            access.check_read(role, identified=False)
            stored = self._store.get(patient_id)
            view = access.deidentify(stored.resource)
            access.assert_deidentified(view)  # defence in depth
            view["version"] = stored.version
            return view, False
        return self._run(actor, role, "export", patient_id, op)

    def history(self, patient_id: str, *, actor: str, role: Role) -> dict:
        """List every stored version (metadata only, no identifiers)."""
        def op():
            access.check_read(role, identified=False)
            entries = self._store.list_history(patient_id)
            return {"patient_id": patient_id,
                    "versions": [{"version": e.version,
                                  "updated_at": e.updated_at}
                                 for e in entries]}, False
        return self._run(actor, role, "history", patient_id, op)

    def read_version(self, patient_id: str, version: int, *,
                     actor: str, role: Role) -> dict:
        """Fetch the snapshot at ``version``.

        Roles with identified-read rights get the full view; other
        export-eligible roles get the de-identified view of that snapshot.
        """
        def op():
            access.check_read(role, identified=False)
            stored = self._store.get_version(patient_id, version)
            if access.can_read_identified(role):
                view = access.identified_view(stored.resource)
            else:
                view = access.deidentify(stored.resource)
                access.assert_deidentified(view)  # defence in depth
            view["version"] = stored.version
            view["updated_at"] = stored.updated_at
            return view, False
        return self._run(actor, role, "read_version", patient_id, op)

    def audit_trail(self) -> list:
        """Forensic accessor; intentionally not exposed via the HTTP API."""
        return self._audit.list_all()

    def close(self) -> None:
        self._store.close()
        self._audit.close()
