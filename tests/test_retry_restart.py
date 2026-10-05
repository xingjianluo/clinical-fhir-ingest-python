"""Retry safety across restarts and interruptions: a retried submission must
replay the original recorded result — never create an extra version, never
double-count the audit, never fail on half-applied state — and the write plus
its idempotency record must commit atomically."""
from __future__ import annotations

import logging

import pytest

from app.errors import ErrorCode, IngestionError
from app.models import Role
from app.service import ClinicalService

log = logging.getLogger("test.retry_restart")

ADMIN = {"actor": "alice", "role": Role.ADMIN}


def payload(pid: str, mrn: str) -> dict:
    return {"patient_id": pid, "identifiers": {"mrn": mrn},
            "observations": [{"code": {"system": "http://loinc.org",
                                       "code": "8480-6"}, "value": mrn}]}


def test_idempotent_retry_after_restart_replays_original_result(tmp_path):
    """The client retries after a timeout; meanwhile the service restarted.
    The retry must replay the first submission's recorded result."""
    db = str(tmp_path / "retry.db")
    key = "submit-restart-1"

    svc = ClinicalService(db)
    first = svc.ingest(payload("p-r", "once"), "fhir-r4",
                       idempotency_key=key, **ADMIN)
    log.info("lifetime 1: ingest(p-r) key=%s -> %s", key, first)
    svc.close()  # service restarts before the client's retry arrives

    svc2 = ClinicalService(db)
    second = svc2.ingest(payload("p-r", "once"), "fhir-r4",
                         idempotency_key=key, **ADMIN)
    log.info("lifetime 2: retried ingest(p-r) key=%s -> %s", key, second)
    assert first == {"patient_id": "p-r", "version": 1}
    assert second == first  # original result replayed, not an error

    # No extra version was created by the retry...
    assert svc2.read("p-r", **ADMIN)["version"] == 1
    assert [v["version"] for v in svc2.history("p-r", **ADMIN)["versions"]] == [1]

    # ...and the audit shows exactly one success for the logical submission
    # plus one replay marker — never two successes.
    successes = [a for a in svc2.audit_trail()
                 if a.action == "ingest" and a.outcome == "success"]
    replays = [a for a in svc2.audit_trail()
               if a.action == "ingest" and a.outcome == "replayed"]
    log.info("decision: success audits=%d replayed audits=%d",
             len(successes), len(replays))
    assert len(successes) == 1
    assert len(replays) == 1
    svc2.close()

    # A further restart still replays: the record is durable.
    svc3 = ClinicalService(db)
    third = svc3.ingest(payload("p-r", "once"), "fhir-r4",
                        idempotency_key=key, **ADMIN)
    assert third == first
    assert svc3.read("p-r", **ADMIN)["version"] == 1
    svc3.close()


def test_idempotent_update_retry_after_restart(tmp_path):
    """Same guarantee for updates: retrying a keyed update after a restart
    must not bump the version a second time."""
    db = str(tmp_path / "retry-upd.db")
    key = "upd-restart-1"

    svc = ClinicalService(db)
    svc.ingest(payload("p-u", "v1"), "fhir-r4", **ADMIN)
    first = svc.update("p-u", payload("p-u", "v2"), "fhir-r4",
                       expected_version=1, idempotency_key=key, **ADMIN)
    log.info("lifetime 1: update(p-u) key=%s -> %s", key, first)
    svc.close()

    svc2 = ClinicalService(db)
    second = svc2.update("p-u", payload("p-u", "v2"), "fhir-r4",
                         expected_version=1, idempotency_key=key, **ADMIN)
    log.info("lifetime 2: retried update(p-u) key=%s -> %s", key, second)
    assert first == {"patient_id": "p-u", "version": 2}
    assert second == first
    assert svc2.read("p-u", **ADMIN)["version"] == 2
    versions = [v["version"] for v in svc2.history("p-u", **ADMIN)["versions"]]
    log.info("decision: history versions=%s (no duplicate v3)", versions)
    assert versions == [1, 2]
    svc2.close()


def test_write_and_idempotency_record_commit_atomically(tmp_path):
    """If the service dies between applying the write and recording the key
    (simulated by forcing the key insert to fail), the write must roll back
    with it — the later retry then performs the write cleanly instead of
    tripping over a half-applied 'already exists' state."""
    db = str(tmp_path / "atomic.db")
    key = "submit-atomic"
    svc = ClinicalService(db)

    original = svc._store._record_key

    def boom(k, fp, resp):
        raise RuntimeError("simulated crash before key commit")

    svc._store._record_key = boom
    with pytest.raises(RuntimeError):
        svc.ingest(payload("p-a", "crashed"), "fhir-r4",
                   idempotency_key=key, **ADMIN)
    svc._store._record_key = original

    # The write rolled back together with the key record: nothing half-applied.
    from app.errors import NotFoundError
    with pytest.raises(NotFoundError):
        svc.read("p-a", **ADMIN)
    log.info("decision: forced crash left no resource row and no key record")

    # The retried submission therefore runs cleanly as a first-class write.
    retried = svc.ingest(payload("p-a", "crashed"), "fhir-r4",
                         idempotency_key=key, **ADMIN)
    log.info("retry after rolled-back attempt -> %s", retried)
    assert retried == {"patient_id": "p-a", "version": 1}
    assert svc.read("p-a", **ADMIN)["version"] == 1
    svc.close()

    # ...and the result is durable across a restart.
    svc2 = ClinicalService(db)
    assert svc2.read("p-a", **ADMIN)["version"] == 1
    replay = svc2.ingest(payload("p-a", "crashed"), "fhir-r4",
                         idempotency_key=key, **ADMIN)
    assert replay == retried
    svc2.close()


def test_failed_submission_does_not_poison_the_key(tmp_path):
    """A submission whose write is rejected (duplicate patient) records no
    key, so the client may reuse the key for the corrected request."""
    db = str(tmp_path / "poison.db")
    svc = ClinicalService(db)
    svc.ingest(payload("p-dup", "first"), "fhir-r4", **ADMIN)

    key = "submit-poison"
    from app.errors import ConcurrencyError
    with pytest.raises(ConcurrencyError) as exc:
        svc.ingest(payload("p-dup", "clash"), "fhir-r4",
                   idempotency_key=key, **ADMIN)
    log.info("duplicate ingest decision: code=%s", exc.value.code)
    assert exc.value.code is ErrorCode.VERSION_CONFLICT

    # The failed attempt recorded nothing: the same key is still usable for
    # the corrected, genuinely-new submission.
    ok = svc.ingest(payload("p-fixed", "fixed"), "fhir-r4",
                    idempotency_key=key, **ADMIN)
    log.info("corrected ingest with reused key -> %s", ok)
    assert ok == {"patient_id": "p-fixed", "version": 1}

    # ...and from then on the key is bound to the corrected request.
    with pytest.raises(IngestionError) as exc2:
        svc.ingest(payload("p-other", "other"), "fhir-r4",
                   idempotency_key=key, **ADMIN)
    assert exc2.value.code is ErrorCode.MALFORMED_PAYLOAD
    svc.close()
