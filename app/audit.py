"""审计记录写入与受控读取。

审计记录独立存放于审计通道（audit/audit.log），普通患者读取接口不会返回它；
list_entries 仅做读取，调用方的角色校验由访问控制层/API 层负责。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from app.models import AuditEntry
from app.storage import LocalStore

logger = logging.getLogger("app.audit")


def utc_now_iso() -> str:
    """UTC 时间，毫秒精度，Z 结尾。"""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def build_entry(
    *,
    actor: str,
    role: str,
    action: str,
    decision: str,
    reason: str,
    resource_type: str = "Patient",
    patient_id: str | None = None,
    rev: int | None = None,
    resource_version: str | None = None,
    detail: dict | None = None,
) -> AuditEntry:
    return AuditEntry(
        audit_id=uuid.uuid4().hex,
        timestamp=utc_now_iso(),
        actor=actor,
        role=role,
        action=action,
        resource_type=resource_type,
        patient_id=patient_id,
        rev=rev,
        resource_version=resource_version,
        decision=decision,
        reason=reason,
        detail=detail or {},
    )


def record(store: LocalStore, entry: AuditEntry) -> None:
    """追加审计，并在应用日志中留一条判定记录（便于单测捕获）。"""
    store.append_audit(entry)
    logger.info(
        "audit actor=%s role=%s action=%s patient=%s rev=%s version=%s decision=%s reason=%s",
        entry.actor,
        entry.role,
        entry.action,
        entry.patient_id,
        entry.rev,
        entry.resource_version,
        entry.decision,
        entry.reason,
    )


def list_entries(
    store: LocalStore, *, actor_role: str, patient_id: str | None = None
) -> list[AuditEntry]:
    """读取审计条目（角色是否允许由调用方在调用前判定）。"""
    entries = store.read_audit(patient_id)
    logger.debug(
        "audit-read role=%s patient_filter=%s returned=%d",
        actor_role,
        patient_id,
        len(entries),
    )
    return entries
