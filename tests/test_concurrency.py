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


def test_concurrent_stale_writes_classified_exactly_one_wins(tmp_path):
    """N clients all submit against the same (soon-stale) version: exactly
    one commits, the rest are rejected as VERSION_CONFLICT with the current
    version in detail; the winning write is immediately readable and still
    there after a restart."""
    db = str(tmp_path / "race.db")
    svc = ClinicalService(db)
    svc.ingest(payload("p-race", "base"), "fhir-r4", **ADMIN)

    wins, conflicts = [], []
    barrier = threading.Barrier(8)

    def worker(i: int):
        barrier.wait()
        try:
            svc.update("p-race", payload("p-race", f"w{i}"), "fhir-r4",
                       expected_version=1, **ADMIN)  # all race on version 1
            wins.append(i)
        except ConcurrencyError as e:
            conflicts.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    log.info("input=8 writers all expecting v1 decision: wins=%s conflicts=%d",
             wins, len(conflicts))
    assert len(wins) == 1                       # only the latest-based commit
    assert len(conflicts) == 7
    for e in conflicts:                         # each rejection explains itself
        assert e.code is ErrorCode.VERSION_CONFLICT
        assert e.detail["expected_version"] == 1
        assert e.detail["current_version"] == 2

    # The committed write is visible to any later read...
    final = svc.read("p-race", **ADMIN)
    assert final["version"] == 2
    assert final["identifiers"]["mrn"] == f"w{wins[0]}"
    assert [v["version"] for v in svc.history("p-race", **ADMIN)["versions"]] == [1, 2]
    svc.close()

    # ...and survives a restart (committed before the success response).
    svc2 = ClinicalService(db)
    after = svc2.read("p-race", **ADMIN)
    log.info("after restart: version=%s mrn=%s",
             after["version"], after["identifiers"]["mrn"])
    assert after == final
    svc2.close()


def test_idempotent_retry_creates_no_extra_version_or_audit(svc):
    """A retried submission (timeout/network) replays the recorded response:
    no new version, no second 'success' audit entry."""
    key = "submit-42"
    first = svc.ingest(payload("p-4", "once"), "fhir-r4",
                       idempotency_key=key, **ADMIN)
    # client retries the exact same submission after a timeout
    second = svc.ingest(payload("p-4", "once"), "fhir-r4",
                        idempotency_key=key, **ADMIN)
    log.info("input=ingest(p-4) key=%s retried decision: first=%s second=%s",
             key, first, second)
    assert first == {"patient_id": "p-4", "version": 1}
    assert second == first                      # same recorded response
    assert svc.read("p-4", **ADMIN)["version"] == 1  # no extra version

    successes = [a for a in svc.audit_trail()
                 if a.action == "ingest" and a.outcome == "success"]
    replays = [a for a in svc.audit_trail()
               if a.action == "ingest" and a.outcome == "replayed"]
    log.info("decision: success audits=%d replayed audits=%d",
             len(successes), len(replays))
    assert len(successes) == 1   # the logical write is audited exactly once
    assert len(replays) == 1     # the retry is visible but not a duplicate


def test_idempotency_key_reused_with_different_payload_rejected(svc):
    key = "submit-43"
    svc.ingest(payload("p-5", "a"), "fhir-r4", idempotency_key=key, **ADMIN)
    from app.errors import IngestionError
    with pytest.raises(IngestionError) as exc:
        svc.ingest(payload("p-5", "DIFFERENT"), "fhir-r4",
                   idempotency_key=key, **ADMIN)
    log.info("input=ingest key=%s different payload decision: code=%s",
             key, exc.value.code)
    assert exc.value.code is ErrorCode.MALFORMED_PAYLOAD


def test_concurrent_same_key_submissions_commit_exactly_once(svc):
    """Two clients racing with the same idempotency key: one commits, the
    other replays — exactly one version and one success audit entry."""
    key = "race-1"
    results, barrier = [], threading.Barrier(2)

    def submit():
        barrier.wait()
        results.append(svc.ingest(payload("p-6", "raced"), "fhir-r4",
                                  idempotency_key=key, **ADMIN))

    threads = [threading.Thread(target=submit) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    log.info("input=2 concurrent same-key submits decision: results=%s", results)
    assert results == [{"patient_id": "p-6", "version": 1}] * 2
    assert svc.read("p-6", **ADMIN)["version"] == 1
    successes = [a for a in svc.audit_trail()
                 if a.action == "ingest" and a.outcome == "success"]
    assert len(successes) == 1


def test_idempotent_update_retry_does_not_bump_version(svc):
    svc.ingest(payload("p-7", "v1"), "fhir-r4", **ADMIN)
    key = "upd-1"
    first = svc.update("p-7", payload("p-7", "v2"), "fhir-r4",
                       expected_version=1, idempotency_key=key, **ADMIN)
    second = svc.update("p-7", payload("p-7", "v2"), "fhir-r4",
                        expected_version=1, idempotency_key=key, **ADMIN)
    log.info("input=update(p-7) key=%s retried decision: first=%s second=%s",
             key, first, second)
    assert first == {"patient_id": "p-7", "version": 2}
    assert second == first
    # without the key the same stale update would be a VERSION_CONFLICT
    assert svc.read("p-7", **ADMIN)["version"] == 2
    history = svc.history("p-7", **ADMIN)
    assert [v["version"] for v in history["versions"]] == [1, 2]
