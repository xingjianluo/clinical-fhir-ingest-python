"""角色访问控制与去标识化导出。

角色矩阵：
  admin     读完整字段、写、读审计
  clinician 读完整字段、写（不读审计）
  analyst   仅去标识化导出（直接标识字段一律不可见），不可写、不可读审计

去标识化规则（analyst 与 /export 通道）：
  移除 patient_id、name；birth_date 归粗到年；不暴露 request_id/时间戳。
  保留 gender、age_band（年龄段）、active、conditions（仅 code/system，无 display 文本）。
"""

from __future__ import annotations

import logging
from datetime import date

from app.models import RECOGNIZED_CODING_SYSTEMS, Patient

logger = logging.getLogger("app.access")

_FULL_READ_ROLES = frozenset({"admin", "clinician"})
_WRITE_ROLES = frozenset({"admin", "clinician"})
_AUDIT_ROLES = frozenset({"admin"})
_EXPORT_ROLES = frozenset({"admin", "analyst"})

# 敏感/直接标识字段，任何无完整读取权限的角色都不能得到它们。
_DIRECT_IDENTIFIERS = frozenset({"patient_id", "name", "request_id"})


def can_read(role: str) -> bool:
    return role in _FULL_READ_ROLES or role in _EXPORT_ROLES


def can_read_full(role: str) -> bool:
    return role in _FULL_READ_ROLES


def can_write(role: str) -> bool:
    return role in _WRITE_ROLES


def can_read_audit(role: str) -> bool:
    return role in _AUDIT_ROLES


def can_export_deidentified(role: str) -> bool:
    return role in _EXPORT_ROLES


def _age_band(birth_date: str, today: date | None = None) -> str:
    """把出生日期换算成 10 岁年龄段，避免精确年龄成为准标识。"""
    today = today or date(2026, 9, 23)
    try:
        born = date.fromisoformat(birth_date)
    except ValueError:
        logger.warning("无法解析 birth_date=%r，年龄段返回 unknown", birth_date)
        return "unknown"
    age = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
    if age < 0:
        return "unknown"
    lo = (age // 10) * 10
    return f"{lo}-{lo + 9}"


def project_for_read(patient: Patient, role: str) -> dict:
    """按角色投影读取结果。

    * admin/clinician：完整内部表示。
    * analyst：等价于去标识化视图（绝不返回直接标识字段）。
    * 其他角色：返回空投影（无任何敏感字段），由上层据此拒绝。
    """
    if role in _FULL_READ_ROLES:
        logger.debug("project role=%s patient_id=%s view=full rev=%s", role, patient.patient_id, patient.rev)
        return patient.to_dict()
    if role in _EXPORT_ROLES:
        logger.debug("project role=%s patient_id=%s view=deidentified rev=%s", role, patient.patient_id, patient.rev)
        return deidentify(patient)
    logger.warning("project role=%s patient_id=%s view=empty（无任何读取权限）", role, patient.patient_id)
    return {}


def deidentify(patient: Patient) -> dict:
    """生成去标识化导出文档：去直接标识、粗化时间、编码仅保留 system/code。"""
    safe_codings = [
        {"system": c.system, "code": c.code}
        for c in patient.conditions
        if c.system in RECOGNIZED_CODING_SYSTEMS
    ]
    doc = {
        "resource_type": patient.resource_type,
        "gender": patient.gender,
        "birth_year": patient.birth_date[:4] if len(patient.birth_date) >= 4 else None,
        "age_band": _age_band(patient.birth_date),
        "active": patient.active,
        "conditions": safe_codings,
    }
    # 防线：确认直接标识字段绝不出现在导出文档中。
    leaked = _DIRECT_IDENTIFIERS.intersection(doc)
    if leaked:  # pragma: no cover - 结构性不变量，正常不可达
        raise RuntimeError(f"去标识化文档泄漏直接标识字段: {sorted(leaked)}")
    return doc
