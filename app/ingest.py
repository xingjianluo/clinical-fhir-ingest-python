"""多版本临床资源的归一化、校验与带版本的乐观并发摄取。

外部表示版本（payload["version"]）兼容范围见 app.models.SUPPORTED_VERSIONS（1.0/1.1）。
内部修订号 rev 由存储层单调递增，用作乐观并发控制：
  * 新建：expected_rev 必须为 None（或 0）；已存在则报 VERSION_CONFLICT。
  * 更新：expected_rev 必须等于当前 rev；不存在报 NOT_FOUND；过期报 VERSION_CONFLICT。
  * 携带相同 request_id 的重试返回首次结果，不产生重复写入（幂等）。
"""

from __future__ import annotations

import logging
from typing import Any

from app import audit
from app.errors import ErrorCategory, IngestError
from app.models import (
    RECOGNIZED_CODING_SYSTEMS,
    RESOURCE_TYPE_PATIENT,
    SUPPORTED_VERSIONS,
    Coding,
    Patient,
)
from app.storage import LocalStore

logger = logging.getLogger("app.ingest")

_REQUIRED_SCALAR_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("patient_id", ("patient_id", "id")),
    ("name", ("name",)),
    ("gender", ("gender",)),
    ("birth_date", ("birth_date", "birthDate")),
)


def _err(
    category: ErrorCategory,
    message: str,
    *,
    raw_hint: Any,
    field: str | None = None,
) -> IngestError:
    """构造错误并打印判定依据（输入与版本号），错误保留原始线索 raw_hint。"""
    logger.warning(
        "reject category=%s field=%s message=%s input_keys=%s version=%s raw_hint=%r",
        category.value,
        field,
        message,
        sorted(raw_hint.keys()) if isinstance(raw_hint, dict) else type(raw_hint).__name__,
        raw_hint.get("version") if isinstance(raw_hint, dict) else None,
        raw_hint,
    )
    return IngestError(category, message, raw_hint=raw_hint, field=field)


def _pick(payload: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in payload:
            return payload[key]
    return None


def _require_version(payload: dict[str, Any]) -> str:
    version = payload.get("version")
    if version is None:
        raise _err(
            ErrorCategory.UNSUPPORTED_VERSION,
            "缺少资源表示版本号 version",
            raw_hint=payload,
            field="version",
        )
    if not isinstance(version, str):
        raise _err(
            ErrorCategory.MALFORMED_INPUT,
            f"version 必须是字符串，实际类型 {type(version).__name__}",
            raw_hint=payload,
            field="version",
        )
    if version not in SUPPORTED_VERSIONS:
        raise _err(
            ErrorCategory.UNSUPPORTED_VERSION,
            f"不支持的资源表示版本 {version!r}，兼容范围 {list(SUPPORTED_VERSIONS)}",
            raw_hint=payload,
            field="version",
        )
    return version


def _require_scalar(payload: dict[str, Any], logical: str, keys: tuple[str, ...]) -> Any:
    value = _pick(payload, keys)
    if value is None or (isinstance(value, str) and value.strip() == ""):
        raise _err(
            ErrorCategory.MISSING_REQUIRED_FIELD,
            f"必填字段缺失: {logical}",
            raw_hint=payload,
            field=logical,
        )
    if not isinstance(value, str):
        raise _err(
            ErrorCategory.MALFORMED_INPUT,
            f"字段 {logical} 必须是非空字符串，实际 {value!r}",
            raw_hint=payload,
            field=logical,
        )
    return value


def _normalize_conditions(payload: dict[str, Any]) -> list[Coding]:
    raw = payload.get("conditions", [])
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise _err(
            ErrorCategory.MALFORMED_INPUT,
            f"conditions 必须是列表，实际类型 {type(raw).__name__}",
            raw_hint=payload,
            field="conditions",
        )
    result: list[Coding] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise _err(
                ErrorCategory.MALFORMED_INPUT,
                f"conditions[{index}] 必须是对象，实际 {item!r}",
                raw_hint=payload,
                field=f"conditions[{index}]",
            )
        system = item.get("system")
        code = item.get("code")
        if not system or not isinstance(system, str):
            raise _err(
                ErrorCategory.MISSING_REQUIRED_FIELD,
                f"conditions[{index}] 缺少编码体系 system",
                raw_hint=payload,
                field=f"conditions[{index}].system",
            )
        if system not in RECOGNIZED_CODING_SYSTEMS:
            raise _err(
                ErrorCategory.UNRECOGNIZED_CODING_SYSTEM,
                f"不可识别的编码体系 {system!r}；允许的体系见 RECOGNIZED_CODING_SYSTEMS",
                raw_hint=payload,
                field=f"conditions[{index}].system",
            )
        if not code or not isinstance(code, str):
            raise _err(
                ErrorCategory.MISSING_REQUIRED_FIELD,
                f"conditions[{index}] 缺少编码 code",
                raw_hint=payload,
                field=f"conditions[{index}].code",
            )
        result.append(Coding(system=system, code=code, display=str(item.get("display", ""))))
    return result


def normalize(payload: dict[str, Any]) -> Patient:
    """把外部多版本表示归一到统一内部模型（不通过即抛带分类的 IngestError）。

    注意：归一化失败一律拒绝，绝不静默丢弃字段或整条输入。
    """
    if not isinstance(payload, dict):
        raise IngestError(
            ErrorCategory.MALFORMED_INPUT,
            f"请求体必须是 JSON 对象，实际类型 {type(payload).__name__}",
            raw_hint=payload,
        )

    resource_type = payload.get("resource_type", RESOURCE_TYPE_PATIENT)
    if resource_type != RESOURCE_TYPE_PATIENT:
        raise _err(
            ErrorCategory.MALFORMED_INPUT,
            f"不支持的资源类型 {resource_type!r}，当前仅支持 Patient",
            raw_hint=payload,
            field="resource_type",
        )

    version = _require_version(payload)
    patient_id = _require_scalar(payload, *_scalar_spec("patient_id"))
    name = _require_scalar(payload, *_scalar_spec("name"))
    gender = _require_scalar(payload, *_scalar_spec("gender"))
    birth_date = _require_scalar(payload, *_scalar_spec("birth_date"))
    conditions = _normalize_conditions(payload)
    active = bool(payload.get("active", True))
    if not isinstance(payload.get("active", True), bool):
        raise _err(
            ErrorCategory.MALFORMED_INPUT,
            "active 必须是布尔值",
            raw_hint=payload,
            field="active",
        )

    patient = Patient(
        patient_id=patient_id,
        resource_type=RESOURCE_TYPE_PATIENT,
        version=version,
        rev=0,
        name=name,
        gender=gender,
        birth_date=birth_date,
        active=active,
        conditions=conditions,
    )
    logger.debug(
        "normalized patient_id=%s version=%s conditions=%d active=%s",
        patient.patient_id,
        patient.version,
        len(patient.conditions),
        patient.active,
    )
    return patient


def _scalar_spec(logical: str) -> tuple[str, tuple[str, ...]]:
    for name, keys in _REQUIRED_SCALAR_FIELDS:
        if name == logical:
            return name, keys
    raise KeyError(logical)


def ingest_resource(
    store: LocalStore,
    payload: dict[str, Any],
    *,
    actor: str,
    role: str,
    expected_rev: int | None = None,
    request_id: str | None = None,
) -> Patient:
    """写入或更新资源，执行乐观并发控制与幂等处理。

    返回落库后的 Patient（含最终 rev）。任何拒绝路径都会写审计。
    """
    # 先归一化；非法输入在触碰存储之前被拒绝（保留原始线索）。
    try:
        candidate = normalize(payload)
    except IngestError as err:
        # 归一化拒绝同样必须留痕（patient_id/version 尽力从原始输入提取）。
        raw_pid = payload.get("patient_id") if isinstance(payload, dict) else None
        raw_version = payload.get("version") if isinstance(payload, dict) else None
        _record_reject(
            store, err, actor, role,
            raw_pid if isinstance(raw_pid, str) else None,
            None,
            raw_version if isinstance(raw_version, str) else None,
            request_id,
        )
        raise
    pid = candidate.patient_id
    version = candidate.version

    with store.patient_lock(pid):
        # 幂等：同一 request_id 的重试直接返回首次写入结果，绝不重复写。
        if request_id:
            prior = store.get_idempotency(pid, request_id)
            if prior is not None:
                logger.info(
                    "idempotent-replay patient_id=%s request_id=%s rev=%d version=%s",
                    pid, request_id, prior.rev, version,
                )
                audit.record(
                    store,
                    audit.build_entry(
                        actor=actor, role=role, action="ingest",
                        resource_type=RESOURCE_TYPE_PATIENT, patient_id=pid,
                        rev=prior.rev, resource_version=version,
                        decision="allow", reason="idempotent_replay",
                        detail={"request_id": request_id},
                    ),
                )
                return prior

        current = store.get_patient(pid)
        creating = expected_rev is None or expected_rev == 0

        if creating:
            if current is not None:
                err = IngestError(
                    ErrorCategory.VERSION_CONFLICT,
                    f"患者 {pid} 已存在（当前 rev={current.rev}），新建需指明 expected_rev",
                    raw_hint=payload,
                )
                _record_reject(store, err, actor, role, pid, None, version, request_id)
                raise err
            new_rev = 1
        else:
            if current is None:
                # 与“过期版本”明确区分：资源根本不存在。
                err = IngestError(
                    ErrorCategory.NOT_FOUND,
                    f"患者 {pid} 不存在，无法基于 expected_rev={expected_rev} 更新",
                    raw_hint=payload,
                )
                _record_reject(store, err, actor, role, pid, expected_rev, version, request_id)
                raise err
            if int(expected_rev) != current.rev:  # type: ignore[arg-type]
                err = IngestError(
                    ErrorCategory.VERSION_CONFLICT,
                    f"过期版本：expected_rev={expected_rev}，当前 rev={current.rev}",
                    raw_hint=payload,
                )
                _record_reject(store, err, actor, role, pid, current.rev, version, request_id)
                raise err
            new_rev = current.rev + 1

        candidate.rev = new_rev
        candidate.request_id = request_id or ""
        candidate.created_at = current.created_at if (current and current.created_at) else audit.utc_now_iso()
        candidate.updated_at = audit.utc_now_iso()
        store.save_patient(candidate)
        if request_id:
            store.put_idempotency(pid, request_id, candidate)

        logger.info(
            "ingest-ok patient_id=%s version=%s new_rev=%d action=%s actor=%s",
            pid, version, new_rev, "create" if creating else "update", actor,
        )
        audit.record(
            store,
            audit.build_entry(
                actor=actor, role=role,
                action="create" if creating else "update",
                resource_type=RESOURCE_TYPE_PATIENT, patient_id=pid,
                rev=new_rev, resource_version=version,
                decision="allow",
                reason="created" if creating else "updated",
                detail={"request_id": request_id} if request_id else {},
            ),
        )
        return candidate


def _record_reject(
    store: LocalStore,
    err: IngestError,
    actor: str,
    role: str,
    patient_id: str,
    rev: int | None,
    version: str | None,
    request_id: str | None,
) -> None:
    audit.record(
        store,
        audit.build_entry(
            actor=actor, role=role, action="ingest",
            resource_type=RESOURCE_TYPE_PATIENT, patient_id=patient_id,
            rev=rev, resource_version=version,
            decision="deny", reason=err.category.value,
            detail={"message": err.message, "request_id": request_id}
            if request_id else {"message": err.message},
        ),
    )
