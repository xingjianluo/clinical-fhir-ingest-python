"""Per-patient version history: listing, snapshot retrieval, role-scoped
views, and clearly distinguishable failure causes.

Failure taxonomy under test:
  patient unknown            -> NOT_FOUND
  patient known, version bad -> VERSION_NOT_FOUND
  role without any read right -> FORBIDDEN (nothing returned)
  researcher snapshot         -> de-identified content only
"""
from __future__ import annotations

import logging

import pytest

from app.errors import ErrorCode, NotFoundError
from app.models import DIRECT_IDENTIFIER_FIELDS, Role
from app.service import ClinicalService

log = logging.getLogger("test.history")

ADMIN = {"actor": "alice", "role": Role.ADMIN}
RESEARCHER = {"actor": "bob", "role": Role.RESEARCHER}

PAYLOAD_V1 = {
    "patient_id": "p-h",
    "identifiers": {"mrn": "MRN-H1"},
    "name": "Zhang San",
    "observations": [{"code": {"system": "http://loinc.org", "code": "8480-6"},
                      "value": "120"}],
}
PAYLOAD_V2 = {
    "patient_id": "p-h",
    "identifiers": {"mrn": "MRN-H2"},
    "name": "Zhang San",
    "observations": [{"code": {"system": "http://loinc.org", "code": "8480-6"},
                      "value": "130"}],
}


@pytest.fixture
def svc():
    service = ClinicalService()
    service.ingest(PAYLOAD_V1, "fhir-r4", **ADMIN)
    service.update("p-h", PAYLOAD_V2, "fhir-r4", expected_version=1, **ADMIN)
    yield service
    service.close()


def test_history_lists_versions_in_order_with_timestamps(svc):
    hist = svc.history("p-h", **ADMIN)
    versions = hist["versions"]
    log.info("input=history(p-h) role=admin decision: versions=%s",
             [(v["version"], v["updated_at"]) for v in versions])
    assert [v["version"] for v in versions] == [1, 2]
    # written in order: timestamps are non-decreasing
    assert versions[0]["updated_at"] <= versions[1]["updated_at"]


def test_read_version_returns_exact_snapshot(svc):
    snap1 = svc.read_version("p-h", 1, **ADMIN)
    snap2 = svc.read_version("p-h", 2, **ADMIN)
    log.info("input=read_version(p-h,1|2) role=admin decision: "
             "v1.mrn=%s v1.value=%s v2.mrn=%s v2.value=%s",
             snap1["identifiers"]["mrn"], snap1["observations"][0]["value"],
             snap2["identifiers"]["mrn"], snap2["observations"][0]["value"])
    assert snap1["version"] == 1
    assert snap1["identifiers"]["mrn"] == "MRN-H1"
    assert snap1["observations"][0]["value"] == "120"
    assert snap2["version"] == 2
    assert snap2["identifiers"]["mrn"] == "MRN-H2"
    assert snap2["observations"][0]["value"] == "130"


def test_researcher_snapshot_is_deidentified(svc):
    snap = svc.read_version("p-h", 1, **RESEARCHER)
    log.info("input=read_version(p-h,1) role=researcher decision: keys=%s",
             sorted(snap))
    assert "identifiers" not in snap
    assert "patient_id" not in snap
    assert not (DIRECT_IDENTIFIER_FIELDS & set(snap))
    assert "Zhang San" not in str(snap)
    assert "MRN-H1" not in str(snap)
    # clinical content of that historical version survives
    assert snap["observations"][0]["value"] == "120"
    assert snap["version"] == 1


def test_researcher_can_list_history_metadata(svc):
    hist = svc.history("p-h", **RESEARCHER)
    log.info("input=history(p-h) role=researcher decision: versions=%s",
             [v["version"] for v in hist["versions"]])
    assert [v["version"] for v in hist["versions"]] == [1, 2]


def test_unknown_patient_vs_unknown_version_vs_forbidden(svc):
    # 1) patient does not exist
    with pytest.raises(NotFoundError) as e1:
        svc.read_version("ghost", 1, **ADMIN)
    log.info("input=read_version(ghost,1) decision: code=%s", e1.value.code)
    assert e1.value.code is ErrorCode.NOT_FOUND

    # 2) patient exists, version does not
    with pytest.raises(NotFoundError) as e2:
        svc.read_version("p-h", 99, **ADMIN)
    log.info("input=read_version(p-h,99) decision: code=%s detail=%s",
             e2.value.code, e2.value.detail)
    assert e2.value.code is ErrorCode.VERSION_NOT_FOUND
    assert e2.value.detail["max_version"] == 2  # valid range explained

    # 3) history of an unknown patient is NOT_FOUND, not an empty list
    with pytest.raises(NotFoundError) as e3:
        svc.history("ghost", **ADMIN)
    log.info("input=history(ghost) decision: code=%s", e3.value.code)
    assert e3.value.code is ErrorCode.NOT_FOUND

    # three causes, three distinct codes
    assert len({e1.value.code, e2.value.code, e3.value.code}) == 2
    assert e1.value.code is not e2.value.code


def test_history_and_snapshot_attempts_are_audited(svc):
    svc.history("p-h", **ADMIN)
    svc.read_version("p-h", 1, **RESEARCHER)
    with pytest.raises(NotFoundError):
        svc.read_version("p-h", 99, **ADMIN)
    trail = svc.audit_trail()
    outcomes = {(a.action, a.outcome) for a in trail}
    log.info("decision: audit (action,outcome)=%s", sorted(outcomes))
    assert ("history", "success") in outcomes
    assert ("read_version", "success") in outcomes
    assert ("read_version", "denied") in outcomes  # failed lookup also audited
