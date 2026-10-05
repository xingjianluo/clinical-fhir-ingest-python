"""Orchestration: authorize -> normalize -> store -> audit.

Every attempt (success or failure) is appended to the audit trail. Errors
propagate as ServiceError subclasses so callers can distinguish causes.

Writes may carry an idempotency key: a retried submission (timeout/network
retry) replays the recorded response without creating a new version and
without recording a second "success" audit entry (the replay itself is
audited with outcome "replayed" so it cannot be mistaken for a new write).
"""
from __future__ import annotations

import hashlib
import json
import os

from app import access, normalize
from app.audit import AuditLog
from app.errors import ServiceError
from app.models import AuditRecord, Role
from app.storage import ResourceStore


def _fingerprint(action: str, *parts: object) -> str:
    """Stable hash identifying one logical submission."""
    canonical = json.dumps([action, *parts], sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


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
        # Idempotent replays are audited as "replayed", never as a second
        # "success" — one logical submission yields exactly one success entry.
        replayed = isinstance(result, dict) and result.pop("_replayed", False)
        outcome = "replayed" if replayed else "success"
        self._record_audit(actor, role, action, patient_id, outcome, "ok")
        return result

    # -- public operations --------------------------------------------------
    def ingest(self, payload: dict, source_version: str, *,
               actor: str, role: Role, idempotency_key: str | None = None) -> dict:
        def op():
            access.check_write(role)
            resource = normalize.normalize(payload, source_version)
            if idempotency_key is None:
                stored = self._store.create(resource)
                return {"patient_id": stored.resource.patient_id,
                        "version": stored.version}
            fp = _fingerprint("ingest", source_version, payload)
            response, replayed = self._store.apply_idempotent(
                idempotency_key, fp,
                lambda: self._store.create(resource, commit=False))
            if replayed:
                return {"_replayed": True, **response}
            return response
        return self._run(actor, role, "ingest",
                         payload.get("patient_id") if isinstance(payload, dict) else None,
                         op)

    def update(self, patient_id: str, payload: dict, source_version: str,
               expected_version: int, *, actor: str, role: Role,
               idempotency_key: str | None = None) -> dict:
        def op():
            access.check_write(role)
            resource = normalize.normalize(payload, source_version)
            resource.patient_id = patient_id
            if idempotency_key is None:
                stored = self._store.update(resource, expected_version)
                return {"patient_id": stored.resource.patient_id,
                        "version": stored.version}
            fp = _fingerprint("update", patient_id, source_version,
                              expected_version, payload)
            response, replayed = self._store.apply_idempotent(
                idempotency_key, fp,
                lambda: self._store.update(resource, expected_version,
                                           commit=False))
            if replayed:
                return {"_replayed": True, **response}
            return response
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

    # -- version history ------------------------------------------------------
    def _history_view(self, stored, role: Role) -> dict:
        """Role-appropriate view of one stored snapshot."""
        if role in (Role.ADMIN, Role.CLINICIAN):
            view = access.identified_view(stored.resource)
        else:
            view = access.deidentify(stored.resource)
            access.assert_deidentified(view)
        view["version"] = stored.version
        view["updated_at"] = stored.updated_at
        return view

    def history(self, patient_id: str, *, actor: str, role: Role) -> dict:
        """List every committed version (oldest first) with write timestamps.

        Role control mirrors read/export: identified roles see identified
        snapshots, other roles see de-identified ones. A missing patient
        raises NOT_FOUND — never confused with VERSION_NOT_FOUND/FORBIDDEN.
        """
        def op():
            access.check_read(role, identified=False)
            versions = self._store.list_history(patient_id)
            return {"patient_id": patient_id,
                    "versions": [self._history_view(s, role) for s in versions]}
        return self._run(actor, role, "history", patient_id, op)

    def read_version(self, patient_id: str, version: int, *,
                     actor: str, role: Role) -> dict:
        """Return the immutable snapshot committed at ``version``."""
        def op():
            access.check_read(role, identified=False)
            stored = self._store.get_version(patient_id, version)
            return self._history_view(stored, role)
        return self._run(actor, role, "read_version", patient_id, op)

    def audit_trail(self) -> list:
        """Forensic accessor; intentionally not exposed via the HTTP API."""
        return self._audit.list_all()

    def close(self) -> None:
        self._store.close()
        self._audit.close()
