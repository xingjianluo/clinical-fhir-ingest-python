"""Persistent local storage: data, version history and audit survive restarts,
and writes continue with correct version numbers after reopening."""
from __future__ import annotations

import logging

from app.models import Role
from app.service import ClinicalService

log = logging.getLogger("test.persistence")

ADMIN = {"actor": "alice", "role": Role.ADMIN}


def payload(pid: str, mrn: str) -> dict:
    return {"patient_id": pid, "identifiers": {"mrn": mrn},
            "observations": [{"code": {"system": "http://loinc.org",
                                       "code": "8480-6"}, "value": mrn}]}


def test_restart_preserves_state_history_and_audit(tmp_path):
    db = str(tmp_path / "clinical.db")

    # First lifetime: ingest two patients, update one of them.
    svc = ClinicalService(db)
    svc.ingest(payload("p-1", "v1"), "fhir-r4", **ADMIN)
    svc.update("p-1", payload("p-1", "v2"), "fhir-r4", expected_version=1, **ADMIN)
    svc.ingest(payload("p-2", "only"), "fhir-r4", **ADMIN)
    audit_before = len(svc.audit_trail())
    log.info("lifetime 1: p-1 at v2, p-2 at v1, audit entries=%d", audit_before)
    svc.close()

    # Second lifetime: reopen the same file — everything must still be there.
    svc2 = ClinicalService(db)
    current = svc2.read("p-1", **ADMIN)
    log.info("lifetime 2: read(p-1) -> version=%s mrn=%s",
             current["version"], current["identifiers"]["mrn"])
    assert current["version"] == 2
    assert current["identifiers"]["mrn"] == "v2"

    # Version history survived the restart, oldest first.
    history = svc2.history("p-1", **ADMIN)
    assert [v["version"] for v in history["versions"]] == [1, 2]
    assert history["versions"][0]["identifiers"]["mrn"] == "v1"
    assert history["versions"][1]["identifiers"]["mrn"] == "v2"
    log.info("lifetime 2: history(p-1) -> versions=%s",
             [v["version"] for v in history["versions"]])

    # Point-in-time snapshot still retrievable after restart.
    snap = svc2.read_version("p-1", 1, **ADMIN)
    assert snap["version"] == 1
    assert snap["identifiers"]["mrn"] == "v1"

    # Audit trail survived too (same db file, append-only table).
    assert len(svc2.audit_trail()) >= audit_before

    # Writes continue from the persisted version, not from scratch.
    resumed = svc2.update("p-1", payload("p-1", "v3"), "fhir-r4",
                          expected_version=2, **ADMIN)
    log.info("lifetime 2: update(p-1, expected=2) -> version=%s",
             resumed["version"])
    assert resumed["version"] == 3
    svc2.close()

    # Third lifetime: the post-restart write is durable as well.
    svc3 = ClinicalService(db)
    final = svc3.read("p-1", **ADMIN)
    assert final["version"] == 3
    assert final["identifiers"]["mrn"] == "v3"
    versions = [v["version"] for v in svc3.history("p-1", **ADMIN)["versions"]]
    log.info("lifetime 3: history(p-1) -> versions=%s", versions)
    assert versions == [1, 2, 3]
    svc3.close()


def test_concurrent_services_share_one_store(tmp_path):
    """Two service instances on the same file see one consistent state."""
    db = str(tmp_path / "shared.db")
    a = ClinicalService(db)
    b = ClinicalService(db)
    a.ingest(payload("p-x", "from-a"), "fhir-r4", **ADMIN)
    # The other instance immediately sees the same current state.
    seen = b.read("p-x", **ADMIN)
    log.info("writer=A reader=B decision: version=%s mrn=%s",
             seen["version"], seen["identifiers"]["mrn"])
    assert seen["identifiers"]["mrn"] == "from-a"
    # ...and B's committed write is visible to A afterwards.
    b.update("p-x", payload("p-x", "from-b"), "fhir-r4", expected_version=1,
             **ADMIN)
    assert a.read("p-x", **ADMIN)["identifiers"]["mrn"] == "from-b"
    a.close()
    b.close()
