"""统一内部模型、版本与角色常量。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Role(str, Enum):
    """调用方角色。"""

    ADMIN = "admin"
    CLINICIAN = "clinician"
    ANALYST = "analyst"


#: 受支持的外部资源表示版本（语义版本主.次；同主版本向后兼容）。
SUPPORTED_VERSIONS: tuple[str, ...] = ("1.0", "1.1")
#: 归一化时可识别的编码体系白名单。
RECOGNIZED_CODING_SYSTEMS: frozenset[str] = frozenset(
    {
        "http://snomed.info/sct",
        "http://hl7.org/fhir/sid/icd-10",
        "http://loinc.org",
    }
)

RESOURCE_TYPE_PATIENT = "Patient"
RESOURCE_TYPES: frozenset[str] = frozenset({RESOURCE_TYPE_PATIENT})


@dataclass
class Coding:
    """统一内部编码模型。"""

    system: str
    code: str
    display: str = ""

    def to_dict(self) -> dict:
        return {"system": self.system, "code": self.code, "display": self.display}

    @classmethod
    def from_dict(cls, data: dict) -> "Coding":
        return cls(
            system=str(data["system"]),
            code=str(data["code"]),
            display=str(data.get("display", "")),
        )


@dataclass
class Patient:
    """统一内部患者模型（归一化后的权威表示）。

    rev 为存储层单调递增的修订号（乐观并发控制用），
    version 为外部资源表示版本（1.0/1.1）。
    """

    patient_id: str
    resource_type: str
    version: str
    rev: int
    name: str
    gender: str
    birth_date: str
    active: bool
    conditions: list[Coding] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    request_id: str = ""

    def to_dict(self) -> dict:
        return {
            "patient_id": self.patient_id,
            "resource_type": self.resource_type,
            "version": self.version,
            "rev": self.rev,
            "name": self.name,
            "gender": self.gender,
            "birth_date": self.birth_date,
            "active": self.active,
            "conditions": [c.to_dict() for c in self.conditions],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "request_id": request_id_or_empty(self.request_id),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Patient":
        return cls(
            patient_id=str(data["patient_id"]),
            resource_type=str(data["resource_type"]),
            version=str(data["version"]),
            rev=int(data["rev"]),
            name=str(data["name"]),
            gender=str(data["gender"]),
            birth_date=str(data["birth_date"]),
            active=bool(data["active"]),
            conditions=[Coding.from_dict(c) for c in data.get("conditions", [])],
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
            request_id=str(data.get("request_id", "")),
        )


def request_id_or_empty(value: str) -> str:
    """request_id 可选字段的读取辅助。"""
    return value if value else ""


@dataclass
class AuditEntry:
    """审计记录（仅可通过专用审计通道获取，不进入普通读取接口）。"""

    audit_id: str
    timestamp: str
    actor: str
    role: str
    action: str
    resource_type: str
    patient_id: str | None
    rev: int | None
    resource_version: str | None
    decision: str
    reason: str
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "audit_id": self.audit_id,
            "timestamp": self.timestamp,
            "actor": self.actor,
            "role": self.role,
            "action": self.action,
            "resource_type": self.resource_type,
            "patient_id": self.patient_id,
            "rev": self.rev,
            "resource_version": self.resource_version,
            "decision": self.decision,
            "reason": self.reason,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AuditEntry":
        return cls(
            audit_id=str(data["audit_id"]),
            timestamp=str(data["timestamp"]),
            actor=str(data["actor"]),
            role=str(data["role"]),
            action=str(data["action"]),
            resource_type=str(data["resource_type"]),
            patient_id=data.get("patient_id"),
            rev=data.get("rev"),
            resource_version=data.get("resource_version"),
            decision=str(data["decision"]),
            reason=str(data["reason"]),
            detail=dict(data.get("detail", {})),
        )
