"""Ingestion rejections: causes must be distinguishable and clues preserved."""
from __future__ import annotations

import logging

import pytest

from app.errors import ErrorCode, IngestionError
from app.models import Role
from app.normalize import normalize
from app.service import ClinicalService

log = logging.getLogger("test.ingest")

VALID_R4 = {
    "patient_id": "p-100",
    "identifiers": {"mrn": "MRN-1"},
    "name": "Zhang San",
    "observations": [
        {"code": {"system": "http://loinc.org", "code": "8480-6"}, "value": "120"}
    ],
}


@pytest.fixture
def svc():
    service = ClinicalService()
    yield service
    service.close()


def test_r4_and_r5_normalize_to_same_model():
    r4 = normalize(VALID_R4, "fhir-r4")
    r5 = normalize({
        "patient_id": "p-100",
        "identifier": [{"type": "mrn", "value": "MRN-1"}],
        "name": "Zhang San",
        "observations": [
            {"code": {"system": "http://loinc.org", "code": "8480-6"},
             "valueString": "120"}
        ],
    }, "fhir-r5")
    log.info("input r4=%s r5 equivalent; decision: compare normalized fields", VALID_R4)
    assert r4.patient_id == r5.patient_id
    assert r4.identifiers == r5.identifiers
    assert [o.code.code for o in r4.observations] == [o.code.code for o in r5.observations]


def test_missing_required_field_rejected():
    bad = {"patient_id": "p-1", "observations": []}  # no mrn
    with pytest.raises(IngestionError) as exc:
        normalize(bad, "fhir-r4")
    log.info("input=%s version=fhir-r4 decision: code=%s detail=%s",
             bad, exc.value.code, exc.value.detail)
    assert exc.value.code is ErrorCode.MISSING_REQUIRED_FIELD
    assert exc.value.detail["field"] == "mrn"  # original clue preserved


def test_unknown_coding_system_rejected():
    bad = {"patient_id": "p-1", "identifiers": {"mrn": "M"},
           "observations": [{"code": {"system": "http://example.org/unknown",
                                      "code": "X"}, "value": "1"}]}
    with pytest.raises(IngestionError) as exc:
        normalize(bad, "fhir-r4")
    log.info("input=%s version=fhir-r4 decision: code=%s detail=%s",
             bad, exc.value.code, exc.value.detail)
    assert exc.value.code is ErrorCode.UNKNOWN_CODING_SYSTEM
    assert exc.value.detail["system"] == "http://example.org/unknown"


def test_unsupported_version_rejected():
    with pytest.raises(IngestionError) as exc:
        normalize(VALID_R4, "fhir-r3")
    log.info("input=<valid r4 payload> version=fhir-r3 decision: code=%s detail=%s",
             exc.value.code, exc.value.detail)
    assert exc.value.code is ErrorCode.UNSUPPORTED_VERSION
    assert "fhir-r4" in exc.value.detail["supported"]


def test_rejection_codes_are_distinguishable(svc):
    cases = [
        ({"patient_id": "p", "observations": []}, "fhir-r4",
         ErrorCode.MISSING_REQUIRED_FIELD),
        ({"patient_id": "p", "identifiers": {"mrn": "M"},
          "observations": [{"code": {"system": "bad", "code": "1"}, "value": "1"}]},
         "fhir-r4", ErrorCode.UNKNOWN_CODING_SYSTEM),
        (VALID_R4, "fhir-r3", ErrorCode.UNSUPPORTED_VERSION),
    ]
    codes = set()
    for payload, version, expected in cases:
        with pytest.raises(IngestionError) as exc:
            svc.ingest(payload, version, actor="alice", role=Role.ADMIN)
        log.info("input=%s version=%s decision: expected=%s got=%s",
                 payload, version, expected, exc.value.code)
        assert exc.value.code is expected
        codes.add(exc.value.code)
    assert len(codes) == 3  # all three causes are mutually distinguishable
    # every rejected attempt was audited
    denied = [a for a in svc.audit_trail() if a.outcome == "denied"]
    assert len(denied) == 3
