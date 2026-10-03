"""Per-patient version history: ordered listing, point-in-time snapshots,
role-controlled views, and clearly distinguished failure causes."""
from __future__ import annotations

import logging

import pytest

from app.errors import (
    AccessDeniedError,
    ErrorCode,
    NotFoundError,
    VersionNotFoundError,
)
from app.models import Role
from app.service import ClinicalService

log = logging.getLogger("test.history")

ADMIN = {"actor": "alice", "role": Role.ADMIN}
RESEARCHER = {"actor": "bob", "role": Role.RESEARCHER}


def payload(pid: str, mrn: str, name: str) -> dict:
    return {"patient_id": pid, "identifiers": {"mrn": mrn}, "name": name,
            "observations": [{"code": {"system": "http://loinc.org",
                                       "code": "8480-6"}, "value": mrn}]}


@pytest.fixture
def svc():
    service = ClinicalService()
    service.ingest(payload("p-h", "m1", "Name One"), "fhir-r4", **ADMIN)
    service.update("p-h", payload("p-h", "m2", "Name Two"), "fhir-r4",
                   expected_version=1, **ADMIN)
    service.update("p-h", payload("p-h", "m3", "Name Three"), "fhir-r4",
                   expected_version=2, **ADMIN)
    yield service
    service.close()


def test_history_lists_versions_in_order_with_timestamps(svc):
    history = svc.history("p-h", **ADMIN)
    versions = history["versions"]
    log.info("decision: versions=%s updated_at=%s",
             [v["version"] for v in versions],
             [v["updated_at"] for v in versions])
    assert [v["version"] for v in versions] == [1, 2, 3]
    # write timestamps are present and non-decreasing
    stamps = [v["updated_at"] for v in versions]
    assert all(isinstance(ts, float) for ts in stamps)
    assert stamps == sorted(stamps)
    # each entry is the snapshot committed at that version
    assert [v["identifiers"]["mrn"] for v in versions] == ["m1", "m2", "m3"]


def test_read_version_returns_immutable_snapshots(svc):
    v1 = svc.read_version("p-h", 1, **ADMIN)
    v3 = svc.read_version("p-h", 3, **ADMIN)
    log.info("decision: v1.mrn=%s v3.mrn=%s",
             v1["identifiers"]["mrn"], v3["identifiers"]["mrn"])
    assert v1["identifiers"]["mrn"] == "m1"
    assert v1["identifiers"]["name"] == "Name One"
    assert v3["identifiers"]["mrn"] == "m3"
    # snapshots are immutable: later writes did not rewrite version 1
    again = svc.read_version("p-h", 1, **ADMIN)
    assert again == v1


def test_history_view_is_role_controlled(svc):
    researcher_view = svc.history("p-h", **RESEARCHER)
    log.info("role=researcher decision: entry keys=%s",
             sorted(researcher_view["versions"][0]))
    for entry in researcher_view["versions"]:
        assert "identifiers" not in entry
        assert "patient_id" not in entry
        assert "Name" not in str(entry)          # no direct identifier leaks
        assert entry["patient_ref"].startswith("anon-")
        assert entry["observations"]              # clinical content kept
    snap = svc.read_version("p-h", 2, **RESEARCHER)
    assert "identifiers" not in snap
    assert snap["version"] == 2


def test_failure_causes_are_distinguishable(svc):
    # 1) patient does not exist -> NOT_FOUND
    with pytest.raises(NotFoundError) as e1:
        svc.history("ghost", **ADMIN)
    log.info("input=history(ghost) decision: code=%s", e1.value.code)
    assert e1.value.code is ErrorCode.NOT_FOUND

    with pytest.raises(NotFoundError) as e2:
        svc.read_version("ghost", 1, **ADMIN)
    assert e2.value.code is ErrorCode.NOT_FOUND

    # 2) patient exists, version does not -> VERSION_NOT_FOUND (distinct code)
    with pytest.raises(VersionNotFoundError) as e3:
        svc.read_version("p-h", 99, **ADMIN)
    log.info("input=read_version(p-h, 99) decision: code=%s detail=%s",
             e3.value.code, e3.value.detail)
    assert e3.value.code is ErrorCode.VERSION_NOT_FOUND
    assert e3.value.detail["version"] == 99

    # 3) identified read by a researcher -> FORBIDDEN (not NOT_FOUND)
    with pytest.raises(AccessDeniedError) as e4:
        svc.read("p-h", **RESEARCHER)
    log.info("input=read(p-h) role=researcher decision: code=%s", e4.value.code)
    assert e4.value.code is ErrorCode.FORBIDDEN

    # all four causes carry different error codes
    codes = {e1.value.code, e2.value.code, e3.value.code, e4.value.code}
    assert len(codes) == 3  # NOT_FOUND shared, VERSION_NOT_FOUND, FORBIDDEN
    assert ErrorCode.VERSION_NOT_FOUND in codes
    assert ErrorCode.FORBIDDEN in codes


def test_history_endpoints_over_http(svc):
    """Route-level check: new endpoints exist and error codes map to the
    documented HTTP statuses with distinguishable bodies."""
    from fastapi.testclient import TestClient

    from main import app, get_service

    app.dependency_overrides[get_service] = lambda: svc
    try:
        client = TestClient(app)
        admin = {"X-Actor": "alice", "X-Role": "admin"}
        researcher = {"X-Actor": "bob", "X-Role": "researcher"}

        r = client.get("/patients/p-h/history", headers=admin)
        log.info("GET /patients/p-h/history -> %s", r.status_code)
        assert r.status_code == 200
        assert [v["version"] for v in r.json()["versions"]] == [1, 2, 3]

        r = client.get("/patients/p-h/versions/2", headers=admin)
        assert r.status_code == 200
        assert r.json()["identifiers"]["mrn"] == "m2"

        # researcher gets de-identified history, not an error
        r = client.get("/patients/p-h/history", headers=researcher)
        assert r.status_code == 200
        assert "identifiers" not in r.json()["versions"][0]

        # missing patient vs missing version: same 404, different codes
        r1 = client.get("/patients/ghost/history", headers=admin)
        r2 = client.get("/patients/p-h/versions/99", headers=admin)
        log.info("ghost -> %s/%s, v99 -> %s/%s",
                 r1.status_code, r1.json()["error"]["code"],
                 r2.status_code, r2.json()["error"]["code"])
        assert r1.status_code == 404
        assert r1.json()["error"]["code"] == "not_found"
        assert r2.status_code == 404
        assert r2.json()["error"]["code"] == "version_not_found"

        # unauthorized role on identified read stays a distinct 403
        r3 = client.get("/patients/p-h", headers=researcher)
        assert r3.status_code == 403
        assert r3.json()["error"]["code"] == "forbidden"
    finally:
        app.dependency_overrides.clear()
