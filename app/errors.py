"""Error taxonomy for ingestion, concurrency and access control.

Every rejection carries a machine-readable ``code`` so callers and tests can
distinguish causes (missing field vs unknown coding system vs unsupported
version vs stale version vs not found vs forbidden).
"""
from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    # Ingestion / validation rejections
    MISSING_REQUIRED_FIELD = "missing_required_field"
    UNKNOWN_CODING_SYSTEM = "unknown_coding_system"
    UNSUPPORTED_VERSION = "unsupported_version"
    MALFORMED_PAYLOAD = "malformed_payload"
    # Concurrency
    VERSION_CONFLICT = "version_conflict"
    NOT_FOUND = "not_found"
    # Access control
    FORBIDDEN = "forbidden"
    UNAUTHENTICATED = "unauthenticated"


class ServiceError(Exception):
    """Base error carrying a distinguishable code and the original clue."""

    def __init__(self, code: ErrorCode, message: str, *, detail: object = None):
        super().__init__(message)
        self.code = code
        self.message = message
        # Original error clue (e.g. offending field, raw fragment) preserved
        # for explainability; never silently dropped.
        self.detail = detail

    def to_dict(self) -> dict:
        return {"code": self.code.value, "message": self.message, "detail": self.detail}


class IngestionError(ServiceError):
    """Input rejected during normalization/validation."""


class ConcurrencyError(ServiceError):
    """Optimistic-concurrency rejection (stale version)."""


class NotFoundError(ServiceError):
    """Resource does not exist (distinct from VERSION_CONFLICT)."""


class AccessDeniedError(ServiceError):
    """Caller role is not allowed to perform the operation."""
