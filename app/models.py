"""Unified internal model that all supported external versions normalize to."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum


class Role(str, Enum):
    ADMIN = "admin"          # full read/write incl. identified data
    CLINICIAN = "clinician"  # read/write clinical data, no export of identifiers
    RESEARCHER = "researcher"  # de-identified export only


# Coding systems this deployment recognises. Anything else is rejected with
# UNKNOWN_CODING_SYSTEM rather than silently dropped.
KNOWN_CODING_SYSTEMS: frozenset[str] = frozenset({
    "http://loinc.org",
    "http://snomed.info/sct",
    "http://hl7.org/fhir/sid/icd-10",
})

# Fields that constitute direct identifiers, stripped on de-identified export.
DIRECT_IDENTIFIER_FIELDS: frozenset[str] = frozenset({
    "name", "birth_date", "mrn", "address", "phone", "email",
})


@dataclass(frozen=True)
class Coding:
    system: str
    code: str
    display: str | None = None


@dataclass(frozen=True)
class ObservationRecord:
    code: Coding
    value: str
    unit: str | None = None
    effective: str | None = None


@dataclass
class PatientResource:
    """Normalized internal representation of a patient resource."""
    patient_id: str
    active: bool = True
    identifiers: dict[str, str] = field(default_factory=dict)  # direct identifiers
    observations: list[ObservationRecord] = field(default_factory=list)


@dataclass
class StoredResource:
    """A PatientResource plus optimistic-concurrency metadata."""
    resource: PatientResource
    version: int
    updated_at: float


@dataclass(frozen=True)
class AuditRecord:
    audit_id: str
    timestamp: float
    actor: str
    role: str
    action: str
    patient_id: str | None
    outcome: str
    detail: str

    @staticmethod
    def new(actor: str, role: str, action: str, patient_id: str | None,
            outcome: str, detail: str) -> AuditRecord:
        return AuditRecord(
            audit_id=str(uuid.uuid4()),
            timestamp=time.time(),
            actor=actor, role=role, action=action,
            patient_id=patient_id, outcome=outcome, detail=detail,
        )
