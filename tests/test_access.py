"""Access control: unauthorized reads leak nothing, exports are de-identified,
and audit records are not reachable through the read/export API."""
from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from app.errors import AccessDeniedError, ErrorCode
from app.models import DIRECT_IDENTIFIER_FIELDS, Role
from app.service import ClinicalService
from main import app, get_service

log = logging.getLogger("test.access")

PAYLOAD = {
    "patient_id": "p-sec",
    "identifiers": {"mrn": "MRN-9"},
    "name": "Zhang San",
    "birth_date": "1980-01-01",
    "phone": "13800000000",
    "observations": [{"code": {"system": "http://loinc.org", "code": "8480-6"},
                      "value": "120"}],
}


@pytest.fixture
def svc():
    service = ClinicalService()
    yield service
    service.close()


@pytest.fixture
def seeded(svc):
    svc.ingest(PAYLOAD, "fhir-r4", actor="alice", role=Role.ADMIN)
    return svc


def test_researcher_cannot_read_identified_data(seeded):
    with pytest.raises(AccessDeniedError) as exc:
        seeded.read("p-sec", actor="bob", role=Role.RESEARCHER)
    log.info("input=read(p-sec) role=researcher decision: code=%s detail=%s",
             exc.value.code, exc.value.detail)
    assert exc.value.code is ErrorCode.FORBIDDEN


def test_researcher_cannot_write(seeded):
    with pytest.raises(AccessDeniedError) as exc:
        seeded.ingest(PAYLOAD, "fhir-r4", actor="bob", role=Role.RESEARCHER)
    log.info("input=ingest role=researcher decision: code=%s", exc.value.code)
    assert exc.value.code is ErrorCode.FORBIDDEN


def test_export_strips_all_direct_identifiers(seeded):
    view = seeded.export("p-sec", actor="bob", role=Role.RESEARCHER)
    log.info("input=export(p-sec) role=researcher decision: view keys=%s",
             sorted(view))
    assert "identifiers" not in view
    assert "patient_id" not in view
    assert not (DIRECT_IDENTIFIER_FIELDS & set(view))
    # no direct identifier value leaks anywhere in the export
    assert "Zhang San" not in str(view)
    assert "MRN-9" not in str(view)
    assert "13800000000" not in str(view)
    # clinical content survives de-identification
    assert view["observations"][0]["code"]["code"] == "8480-6"


def test_clinician_read_returns_identified_view(seeded):
    view = seeded.read("p-sec", actor="carol", role=Role.CLINICIAN)
    log.info("input=read(p-sec) role=clinician decision: identifiers visible=%s",
             "identifiers" in view)
    assert view["identifiers"]["mrn"] == "MRN-9"


def test_every_attempt_is_audited(seeded):
    try:
        seeded.read("p-sec", actor="bob", role=Role.RESEARCHER)
    except AccessDeniedError:
        pass
    seeded.export("p-sec", actor="bob", role=Role.RESEARCHER)
    trail = seeded.audit_trail()
    log.info("decision: audit entries=%d actions=%s", len(trail),
             [(a.action, a.outcome) for a in trail])
    outcomes = {(a.action, a.outcome) for a in trail}
    assert ("read", "denied") in outcomes      # denied attempt recorded
    assert ("export", "success") in outcomes   # allowed attempt recorded
    assert ("ingest", "success") in outcomes


def test_audit_not_exposed_via_http_api(seeded):
    app.dependency_overrides[get_service] = lambda: seeded
    try:
        client = TestClient(app)
        headers = {"X-Actor": "alice", "X-Role": "admin"}
        for path in ("/audit", "/patients/p-sec/audit", "/patients/p-sec",
                     "/patients/p-sec/export"):
            resp = client.get(path, headers=headers)
            log.info("input=GET %s decision: status=%s", path, resp.status_code)
            body = resp.text
            # no audit content (actor/action/outcome tuples) leaks via any route
            assert "denied" not in body or resp.status_code == 404
            if resp.status_code == 200:
                assert "audit_id" not in body
        assert client.get("/audit", headers=headers).status_code == 404
    finally:
        app.dependency_overrides.clear()
