"""Optimistic concurrency: stale versions rejected, retries safe, no lost updates."""
from __future__ import annotations

import logging
import threading

import pytest

from app.errors import ConcurrencyError, ErrorCode, NotFoundError
from app.models import Role
from app.service import ClinicalService

log = logging.getLogger("test.concurrency")

ADMIN = {"actor": "alice", "role": Role.ADMIN}


def payload(pid: str, mrn: str) -> dict:
    return {"patient_id": pid, "identifiers": {"mrn": mrn},
            "observations": [{"code": {"system": "http://loinc.org",
                                       "code": "8480-6"}, "value": mrn}]}


@pytest.fixture
def svc():
    service = ClinicalService()
    yield service
    service.close()


def test_stale_version_rejected_and_distinguished_from_not_found(svc):
    created = svc.ingest(payload("p-1", "v1"), "fhir-r4", **ADMIN)
    log.info("input=%s version=%s decision: created", payload("p-1", "v1"),
             created["version"])

    svc.update("p-1", payload("p-1", "v2"), "fhir-r4",
               expected_version=created["version"], **ADMIN)

    # Replay of the old version must be a VERSION_CONFLICT...
    with pytest.raises(ConcurrencyError) as exc:
        svc.update("p-1", payload("p-1", "v1-again"), "fhir-r4",
                   expected_version=created["version"], **ADMIN)
    log.info("input=%s version=%s decision: code=%s detail=%s",
             payload("p-1", "v1-again"), created["version"],
             exc.value.code, exc.value.detail)
    assert exc.value.code is ErrorCode.VERSION_CONFLICT
    assert exc.value.detail["current_version"] == 2

    # ...which is a different cause from updating a missing resource.
    with pytest.raises(NotFoundError) as exc2:
        svc.update("ghost", payload("ghost", "x"), "fhir-r4",
                   expected_version=1, **ADMIN)
    log.info("input=ghost version=1 decision: code=%s", exc2.value.code)
    assert exc2.value.code is ErrorCode.NOT_FOUND


def test_retry_after_conflict_does_not_duplicate_or_lose_updates(svc):
    created = svc.ingest(payload("p-2", "a"), "fhir-r4", **ADMIN)
    # Two writers read the same version; writer 1 commits first.
    svc.update("p-2", payload("p-2", "writer1"), "fhir-r4",
               expected_version=created["version"], **ADMIN)
    # Writer 2's stale write is rejected...
    with pytest.raises(ConcurrencyError):
        svc.update("p-2", payload("p-2", "writer2"), "fhir-r4",
                   expected_version=created["version"], **ADMIN)
    # ...and its retry re-reads the current version, then succeeds exactly once.
    current = svc.read("p-2", **ADMIN)
    log.info("retry input=%s version=%s decision: re-read current version",
             payload("p-2", "writer2"), current["version"])
    retried = svc.update("p-2", payload("p-2", "writer2"), "fhir-r4",
                         expected_version=current["version"], **ADMIN)
    final = svc.read("p-2", **ADMIN)
    log.info("decision: final version=%s mrn=%s", final["version"],
             final["identifiers"]["mrn"])
    assert retried["version"] == 3
    assert final["version"] == 3            # no duplicate write
    assert final["identifiers"]["mrn"] == "writer2"  # no lost update


def test_concurrent_writers_exactly_one_wins_per_version(svc):
    svc.ingest(payload("p-3", "base"), "fhir-r4", **ADMIN)
    wins, conflicts = [], []
    barrier = threading.Barrier(10)

    def worker(i: int):
        barrier.wait()  # maximise contention
        try:
            current = svc.read("p-3", **ADMIN)
            svc.update("p-3", payload("p-3", f"w{i}"), "fhir-r4",
                       expected_version=current["version"], **ADMIN)
            wins.append(i)
        except ConcurrencyError as e:
            conflicts.append(e.detail["expected_version"])

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    final = svc.read("p-3", **ADMIN)
    log.info("input=10 concurrent writers decision: wins=%d conflicts=%d "
             "final_version=%s", len(wins), len(conflicts), final["version"])
    assert len(wins) + len(conflicts) == 10
    assert len(wins) >= 1
    # version advanced exactly once per winning write: no lost, no duplicate
    assert final["version"] == 1 + len(wins)
