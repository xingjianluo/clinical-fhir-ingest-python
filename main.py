"""HTTP API for the clinical ingestion and controlled-access service.

Caller identity is supplied via headers (demo-grade auth):
  X-Actor: free-form caller name
  X-Role:  admin | clinician | researcher
"""
from __future__ import annotations

import os

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse

from app.errors import ErrorCode, ServiceError
from app.models import Role
from app.service import ClinicalService

_STATUS_BY_CODE = {
    ErrorCode.MISSING_REQUIRED_FIELD: 422,
    ErrorCode.UNKNOWN_CODING_SYSTEM: 422,
    ErrorCode.UNSUPPORTED_VERSION: 422,
    ErrorCode.MALFORMED_PAYLOAD: 400,
    ErrorCode.VERSION_CONFLICT: 409,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.VERSION_NOT_FOUND: 404,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.UNAUTHENTICATED: 401,
}

app = FastAPI(title="clinical-fhir-ingest")

_service: ClinicalService | None = None


def get_service() -> ClinicalService:
    global _service
    if _service is None:
        _service = ClinicalService(os.environ.get("CLINICAL_DB", ":memory:"))
    return _service


@app.exception_handler(ServiceError)
async def service_error_handler(_: Request, exc: ServiceError) -> JSONResponse:
    return JSONResponse(status_code=_STATUS_BY_CODE[exc.code],
                        content={"error": exc.to_dict()})


def _caller(x_actor: str | None, x_role: str | None) -> tuple[str, Role]:
    if not x_actor or not x_role:
        raise ServiceError(ErrorCode.UNAUTHENTICATED,
                           "X-Actor and X-Role headers are required")
    try:
        role = Role(x_role)
    except ValueError:
        raise ServiceError(ErrorCode.UNAUTHENTICATED,
                           f"unknown role '{x_role}'",
                           detail={"role": x_role}) from None
    return x_actor, role


@app.get("/")
def read_root():
    return {"service": "clinical-fhir-ingest", "status": "ok"}


@app.post("/patients", status_code=201)
def ingest(payload: dict, source_version: str,
           x_actor: str | None = Header(None),
           x_role: str | None = Header(None),
           idempotency_key: str | None = Header(None),
           svc: ClinicalService = Depends(get_service)):
    actor, role = _caller(x_actor, x_role)
    return svc.ingest(payload, source_version, actor=actor, role=role,
                      request_id=idempotency_key)


@app.put("/patients/{patient_id}")
def update(patient_id: str, payload: dict, source_version: str,
           expected_version: int,
           x_actor: str | None = Header(None),
           x_role: str | None = Header(None),
           idempotency_key: str | None = Header(None),
           svc: ClinicalService = Depends(get_service)):
    actor, role = _caller(x_actor, x_role)
    return svc.update(patient_id, payload, source_version, expected_version,
                      actor=actor, role=role, request_id=idempotency_key)


@app.get("/patients/{patient_id}")
def read(patient_id: str,
         x_actor: str | None = Header(None),
         x_role: str | None = Header(None),
         svc: ClinicalService = Depends(get_service)):
    actor, role = _caller(x_actor, x_role)
    return svc.read(patient_id, actor=actor, role=role)


@app.get("/patients/{patient_id}/export")
def export(patient_id: str,
           x_actor: str | None = Header(None),
           x_role: str | None = Header(None),
           svc: ClinicalService = Depends(get_service)):
    actor, role = _caller(x_actor, x_role)
    return svc.export(patient_id, actor=actor, role=role)


@app.get("/patients/{patient_id}/history")
def history(patient_id: str,
            x_actor: str | None = Header(None),
            x_role: str | None = Header(None),
            svc: ClinicalService = Depends(get_service)):
    actor, role = _caller(x_actor, x_role)
    return svc.history(patient_id, actor=actor, role=role)


@app.get("/patients/{patient_id}/history/{version}")
def read_version(patient_id: str, version: int,
                 x_actor: str | None = Header(None),
                 x_role: str | None = Header(None),
                 svc: ClinicalService = Depends(get_service)):
    actor, role = _caller(x_actor, x_role)
    return svc.read_version(patient_id, version, actor=actor, role=role)
