"""Orchestration: authorize -> normalize -> store -> audit.

Every attempt (success or failure) is appended to the audit trail. Errors
propagate as ServiceError subclasses so callers can distinguish causes.
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
        try:
            result = fn()
        except ServiceError as e:
            self._record_audit(actor, role, action, patient_id, "denied",
                        f"{e.code.value}: {e.message}")
            raise
        self._record_audit(actor, role, action, patient_id, "success", "ok")
        return result

    # -- public operations --------------------------------------------------
    def ingest(self, payload: dict, source_version: str, *,
               actor: str, role: Role) -> dict:
        def op():
            access.check_write(role)
            resource = normalize.normalize(payload, source_version)
            stored = self._store.create(resource)
            return {"patient_id": stored.resource.patient_id,
                    "version": stored.version}
        return self._run(actor, role, "ingest",
                         payload.get("patient_id") if isinstance(payload, dict) else None,
                         op)

    def update(self, patient_id: str, payload: dict, source_version: str,
               expected_version: int, *, actor: str, role: Role) -> dict:
        def op():
            access.check_write(role)
            resource = normalize.normalize(payload, source_version)
            resource.patient_id = patient_id
            stored = self._store.update(resource, expected_version)
            return {"patient_id": stored.resource.patient_id,
                    "version": stored.version}
        return self._run(actor, role, "update", patient_id, op)

    def read(self, patient_id: str, *, actor: str, role: Role) -> dict:
        def op():
            access.check_read(role, identified=True)
            stored = self._store.get(patient_id)
            view = access.identified_view(stored.resource)
            view["version"] = stored.version
            return view
        return self._run(actor, role, "read", patient_id, op)

    def export(self, patient_id: str, *, actor: str, role: Role) -> dict:
        def op():
            access.check_read(role, identified=False)
            stored = self._store.get(patient_id)
            view = access.deidentify(stored.resource)
            access.assert_deidentified(view)  # defence in depth
            view["version"] = stored.version
            return view
        return self._run(actor, role, "export", patient_id, op)

    def audit_trail(self) -> list:
        """Forensic accessor; intentionally not exposed via the HTTP API."""
        return self._audit.list_all()

    def close(self) -> None:
        self._store.close()
        self._audit.close()
