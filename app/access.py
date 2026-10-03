"""Role-based access control and de-identification.

Rules:
  - write (ingest/update): admin, clinician
  - identified read:       admin, clinician
  - de-identified export:  admin, clinician, researcher
  - researcher never sees direct identifiers; unauthorized reads raise
    AccessDeniedError and return nothing.
"""
from __future__ import annotations

import hashlib

from app.errors import AccessDeniedError, ErrorCode
from app.models import DIRECT_IDENTIFIER_FIELDS, PatientResource, Role

_WRITE_ROLES: frozenset[Role] = frozenset({Role.ADMIN, Role.CLINICIAN})
_IDENTIFIED_READ_ROLES: frozenset[Role] = frozenset({Role.ADMIN, Role.CLINICIAN})
_EXPORT_ROLES: frozenset[Role] = frozenset({Role.ADMIN, Role.CLINICIAN, Role.RESEARCHER})


def check_read(role: Role, *, identified: bool) -> None:
    """Raise AccessDeniedError if the role may not perform this read."""
    allowed = _IDENTIFIED_READ_ROLES if identified else _EXPORT_ROLES
    if role not in allowed:
        raise AccessDeniedError(
            ErrorCode.FORBIDDEN,
            f"role '{role.value}' may not perform "
            f"{'identified read' if identified else 'export'}",
            detail={"role": role.value, "identified": identified},
        )


def can_read_identified(role: Role) -> bool:
    """True if the role may see direct identifiers (else de-identified only)."""
    return role in _IDENTIFIED_READ_ROLES


def check_write(role: Role) -> None:
    """Raise AccessDeniedError if the role may not write."""
    if role not in _WRITE_ROLES:
        raise AccessDeniedError(
            ErrorCode.FORBIDDEN,
            f"role '{role.value}' may not write resources",
            detail={"role": role.value},
        )


def deidentify(resource: PatientResource) -> dict:
    """Return an export view with all direct identifiers removed.

    The export keeps only clinical content; every key in
    DIRECT_IDENTIFIER_FIELDS (and the whole identifiers map) is stripped.
    """
    pseudonym = hashlib.sha256(resource.patient_id.encode()).hexdigest()[:12]
    return {
        "patient_ref": f"anon-{pseudonym}",
        "active": resource.active,
        "observations": [
            {
                "code": {"system": o.code.system, "code": o.code.code,
                         "display": o.code.display},
                "value": o.value,
                "unit": o.unit,
                "effective": o.effective,
            }
            for o in resource.observations
        ],
    }


def identified_view(resource: PatientResource) -> dict:
    """Full view for roles allowed to see direct identifiers."""
    return {
        "patient_id": resource.patient_id,
        "active": resource.active,
        "identifiers": dict(resource.identifiers),
        "observations": [
            {
                "code": {"system": o.code.system, "code": o.code.code,
                         "display": o.code.display},
                "value": o.value,
                "unit": o.unit,
                "effective": o.effective,
            }
            for o in resource.observations
        ],
    }


def assert_deidentified(view: dict) -> None:
    """Defence-in-depth: refuse to emit a view containing direct identifiers."""
    leaked = DIRECT_IDENTIFIER_FIELDS & set(view)
    if leaked or "identifiers" in view or "patient_id" in view:
        raise AccessDeniedError(
            ErrorCode.FORBIDDEN,
            "export view still contains direct identifiers",
            detail={"leaked_fields": sorted(leaked)},
        )
