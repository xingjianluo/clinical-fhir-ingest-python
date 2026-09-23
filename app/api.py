"""FastAPI 路由与 HTTP 边界。

身份通过请求头传入（本地演示用，非生产认证方案）：
  X-Actor：调用方标识（默认 anonymous）
  X-Role：admin / clinician / analyst（缺失或未知按无权限处理）

错误到 HTTP 状态码：
  missing_required_field / malformed_input / unrecognized_coding_system /
  unsupported_version -> 422（拒绝原因在 body.category 中可区分）
  version_conflict -> 409；not_found -> 404；forbidden -> 403
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

from app import access, audit, ingest
from app.errors import ErrorCategory, IngestError
from app.storage import LocalStore

logger = logging.getLogger("app.api")

DEFAULT_DATA_DIR = Path("data")

_STATUS_MAP: dict[ErrorCategory, int] = {
    ErrorCategory.MISSING_REQUIRED_FIELD: 422,
    ErrorCategory.MALFORMED_INPUT: 422,
    ErrorCategory.UNRECOGNIZED_CODING_SYSTEM: 422,
    ErrorCategory.UNSUPPORTED_VERSION: 422,
    ErrorCategory.VERSION_CONFLICT: 409,
    ErrorCategory.NOT_FOUND: 404,
    ErrorCategory.FORBIDDEN: 403,
    ErrorCategory.IDEMPOTENCY_REPLAY: 200,
}


@dataclass
class Caller:
    actor: str
    role: str


def _caller(x_actor: str | None, x_role: str | None) -> Caller:
    return Caller(actor=x_actor or "anonymous", role=(x_role or "").strip().lower())


def create_app(data_dir: str | Path = DEFAULT_DATA_DIR) -> FastAPI:
    store = LocalStore(data_dir)
    store.setup()

    app = FastAPI(
        title="临床资源摄取与受控访问服务",
        version="0.1.0",
    )
    app.state.store = store

    @app.exception_handler(IngestError)
    async def _on_ingest_error(request: Request, exc: IngestError) -> JSONResponse:
        return _ingest_error_handler(request, exc)

    @app.exception_handler(Exception)
    async def _on_unexpected(request: Request, exc: Exception) -> JSONResponse:  # pragma: no cover
        logger.exception("unhandled error")
        return JSONResponse(
            status_code=500,
            content={"category": "internal_error", "message": str(exc)},
        )

    @app.post("/resources/Patient")
    async def upsert_patient(
        body: dict[str, Any],
        request: Request,
        x_expected_rev: int | None = Header(default=None),
        x_request_id: str | None = Header(default=None),
        x_actor: str | None = Header(default=None),
        x_role: str | None = Header(default=None),
    ) -> dict:
        caller = _caller(x_actor, x_role)
        if not access.can_write(caller.role):
            err = IngestError(
                ErrorCategory.FORBIDDEN,
                f"角色 {caller.role or '<empty>'!r} 无权写入临床资源",
                raw_hint=_safe_body(body),
            )
            audit.record(
                store,
                audit.build_entry(
                    actor=caller.actor, role=caller.role, action="ingest",
                    patient_id=body.get("patient_id") if isinstance(body, dict) else None,
                    resource_version=body.get("version") if isinstance(body, dict) else None,
                    decision="deny", reason="forbidden",
                ),
            )
            raise err
        patient = ingest.ingest_resource(
            store,
            body,
            actor=caller.actor,
            role=caller.role,
            expected_rev=x_expected_rev,
            request_id=x_request_id,
        )
        return patient.to_dict()

    @app.get("/resources/Patient/{patient_id}")
    async def read_patient(
        patient_id: str,
        x_actor: str | None = Header(default=None),
        x_role: str | None = Header(default=None),
    ) -> dict:
        caller = _caller(x_actor, x_role)
        if not access.can_read(caller.role):
            _deny_read(store, caller, patient_id)
        patient = store.get_patient(patient_id)
        if patient is None:
            err = IngestError(ErrorCategory.NOT_FOUND, f"患者 {patient_id} 不存在")
            audit.record(
                store,
                audit.build_entry(
                    actor=caller.actor, role=caller.role, action="read",
                    patient_id=patient_id, decision="deny", reason="not_found",
                ),
            )
            raise err
        view = access.project_for_read(patient, caller.role)
        audit.record(
            store,
            audit.build_entry(
                actor=caller.actor, role=caller.role, action="read",
                patient_id=patient_id, rev=patient.rev,
                resource_version=patient.version, decision="allow",
                reason="full" if access.can_read_full(caller.role) else "deidentified",
            ),
        )
        return view

    @app.get("/resources/Patient/{patient_id}/revisions")
    async def list_revisions(
        patient_id: str,
        x_actor: str | None = Header(default=None),
        x_role: str | None = Header(default=None),
    ) -> dict:
        caller = _caller(x_actor, x_role)
        if not access.can_read_full(caller.role):
            _deny_read(store, caller, patient_id, action="read_history")
        revs = store.list_history_revs(patient_id)
        if not revs:
            err = IngestError(ErrorCategory.NOT_FOUND, f"患者 {patient_id} 不存在或无历史")
            raise err
        audit.record(
            store,
            audit.build_entry(
                actor=caller.actor, role=caller.role, action="read_history",
                patient_id=patient_id, decision="allow",
                reason="revisions_listed", detail={"revs": revs},
            ),
        )
        return {"patient_id": patient_id, "revisions": revs}

    @app.get("/resources/Patient/{patient_id}/export")
    async def export_patient(
        patient_id: str,
        x_actor: str | None = Header(default=None),
        x_role: str | None = Header(default=None),
    ) -> dict:
        caller = _caller(x_actor, x_role)
        if not access.can_export_deidentified(caller.role):
            _deny_read(store, caller, patient_id, action="export")
        patient = store.get_patient(patient_id)
        if patient is None:
            err = IngestError(ErrorCategory.NOT_FOUND, f"患者 {patient_id} 不存在")
            audit.record(
                store,
                audit.build_entry(
                    actor=caller.actor, role=caller.role, action="export",
                    patient_id=patient_id, decision="deny", reason="not_found",
                ),
            )
            raise err
        doc = access.deidentify(patient)
        audit.record(
            store,
            audit.build_entry(
                actor=caller.actor, role=caller.role, action="export",
                patient_id=patient_id, rev=patient.rev,
                resource_version=patient.version, decision="allow",
                reason="deidentified_export",
            ),
        )
        return doc

    @app.get("/audit/entries")
    async def get_audit(
        patient_id: str | None = None,
        x_actor: str | None = Header(default=None),
        x_role: str | None = Header(default=None),
    ) -> dict:
        caller = _caller(x_actor, x_role)
        if not access.can_read_audit(caller.role):
            # 审计通道独立：无权限者连审计是否存在都无法通过普通读取获知。
            audit.record(
                store,
                audit.build_entry(
                    actor=caller.actor, role=caller.role, action="read_audit",
                    patient_id=patient_id, decision="deny", reason="forbidden",
                ),
            )
            raise IngestError(
                ErrorCategory.FORBIDDEN,
                f"角色 {caller.role or '<empty>'!r} 无权读取审计记录",
            )
        entries = audit.list_entries(store, actor_role=caller.role, patient_id=patient_id)
        return {"entries": [e.to_dict() for e in entries]}

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    return app


def _deny_read(
    store: LocalStore,
    caller: Caller,
    patient_id: str,
    *,
    action: str = "read",
) -> None:
    audit.record(
        store,
        audit.build_entry(
            actor=caller.actor, role=caller.role, action=action,
            patient_id=patient_id, decision="deny", reason="forbidden",
        ),
    )
    raise IngestError(
        ErrorCategory.FORBIDDEN,
        f"角色 {caller.role or '<empty>'!r} 无权{('执行 ' + action + ' 操作') if action != 'read' else '读取该资源'}",
    )


def _safe_body(body: Any) -> Any:
    """错误返回中原样保留输入线索（本地演示；生产可按需脱敏）。"""
    return body


def _ingest_error_handler(request: Request, exc: IngestError) -> JSONResponse:
    status_code = _STATUS_MAP.get(exc.category, 400)
    logger.warning(
        "http-error path=%s category=%s field=%s status=%d message=%s",
        request.url.path, exc.category.value, exc.field, status_code, exc.message,
    )
    return JSONResponse(
        status_code=status_code,
        content={
            "category": exc.category.value,
            "message": exc.message,
            "field": exc.field,
            "raw_hint": exc.raw_hint,
        },
    )
