"""Durability across restarts and idempotent retries.

Covers:
  - data, version chain and audit trail survive a service restart
    (close + reopen on the same local database file);
  - writes continue from the pre-restart version after reopening;
  - a retried submission (same request_id) neither creates an extra
    version nor duplicates the audit record.
"""
from __future__ import annotations

import logging

import pytest

from app.models import Role
from app.service import ClinicalService

log = logging.getLogger("test.persistence")

ADMIN = {"actor": "alice", "role": Role.ADMIN}


def payload(pid: str, mrn: str) -> dict:
    return {"patient_id": pid, "identifiers": {"mrn": mrn},
            "observations": [{"code": {"system": "http://loinc.org",
                                       "code": "8480-6"}, "value": mrn}]}


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "clinical.db")


def test_restart_preserves_data_versions_and_audit(db_path):
    svc = ClinicalService(db_path)
    svc.ingest(payload("p-1", "v1"), "fhir-r4", **ADMIN)
    svc.update("p-1", payload("p-1", "v2"), "fhir-r4", expected_version=1, **ADMIN)
    audit_before = len(svc.audit_trail())
    svc.close()
    log.info("decision: wrote v1,v2 and %d audit entries, then closed (restart)",
             audit_before)

    # Reopen on the same file: simulates the service process restarting.
    svc2 = ClinicalService(db_path)
    try:
        current = svc2.read("p-1", **ADMIN)
        log.info("input=read(p-1) after restart decision: version=%s mrn=%s",
                 current["version"], current["identifiers"]["mrn"])
        assert current["version"] == 2
        assert current["identifiers"]["mrn"] == "v2"

        # History chain is intact across the restart.
        hist = svc2.history("p-1", **ADMIN)
        log.info("input=history(p-1) after restart decision: versions=%s",
                 [v["version"] for v in hist["versions"]])
        assert [v["version"] for v in hist["versions"]] == [1, 2]

        # Audit trail survived too.
        assert len(svc2.audit_trail()) >= audit_before

        # Writing continues from the pre-restart version, no chain break.
        svc2.update("p-1", payload("p-1", "v3"), "fhir-r4",
                    expected_version=2, **ADMIN)
        again = svc2.read("p-1", **ADMIN)
        log.info("input=update(expected=2) after restart decision: version=%s",
                 again["version"])
        assert again["version"] == 3
        assert again["identifiers"]["mrn"] == "v3"
    finally:
        svc2.close()


def test_restart_preserves_version_snapshots(db_path):
    svc = ClinicalService(db_path)
    svc.ingest(payload("p-2", "first"), "fhir-r4", **ADMIN)
    svc.update("p-2", payload("p-2", "second"), "fhir-r4",
               expected_version=1, **ADMIN)
    svc.close()

    svc2 = ClinicalService(db_path)
    try:
        snap1 = svc2.read_version("p-2", 1, **ADMIN)
        snap2 = svc2.read_version("p-2", 2, **ADMIN)
        log.info("input=read_version(p-2, 1|2) after restart decision: "
                 "v1.mrn=%s v2.mrn=%s",
                 snap1["identifiers"]["mrn"], snap2["identifiers"]["mrn"])
        assert snap1["identifiers"]["mrn"] == "first"
        assert snap2["identifiers"]["mrn"] == "second"
    finally:
        svc2.close()


def test_retried_update_replays_without_extra_version_or_audit(db_path):
    svc = ClinicalService(db_path)
    try:
        svc.ingest(payload("p-3", "base"), "fhir-r4", **ADMIN)

        first = svc.update("p-3", payload("p-3", "committed"), "fhir-r4",
                           expected_version=1, request_id="req-42", **ADMIN)
        # Same submission retried after a timeout: identical request_id.
        retry = svc.update("p-3", payload("p-3", "committed"), "fhir-r4",
                           expected_version=1, request_id="req-42", **ADMIN)
        log.info("input=update(req-42) x2 decision: first=%s retry=%s",
                 first, retry)
        assert retry == first  # same recorded outcome, not a new version

        # Count immediately after the retry, before reads add their own
        # audit entries.
        update_audits = [a for a in svc.audit_trail() if a.action == "update"]
        current = svc.read("p-3", **ADMIN)
        hist = svc.history("p-3", **ADMIN)
        log.info("decision: version=%s history=%s update_audit_entries=%d",
                 current["version"],
                 [v["version"] for v in hist["versions"]],
                 len(update_audits))
        assert current["version"] == 2                     # no phantom version
        assert [v["version"] for v in hist["versions"]] == [1, 2]
        assert len(update_audits) == 1                     # no duplicate audit
    finally:
        svc.close()


def test_retried_ingest_replays_instead_of_conflicting(db_path):
    svc = ClinicalService(db_path)
    try:
        first = svc.ingest(payload("p-4", "once"), "fhir-r4",
                           request_id="req-create-1", **ADMIN)
        retry = svc.ingest(payload("p-4", "once"), "fhir-r4",
                           request_id="req-create-1", **ADMIN)
        log.info("input=ingest(req-create-1) x2 decision: first=%s retry=%s",
                 first, retry)
        assert retry == first == {"patient_id": "p-4", "version": 1}
        hist = svc.history("p-4", **ADMIN)
        assert [v["version"] for v in hist["versions"]] == [1]
        successes = [a for a in svc.audit_trail()
                     if a.action == "ingest" and a.outcome == "success"]
        assert len(successes) == 1  # audited once, not twice
    finally:
        svc.close()


def test_replay_outcome_survives_restart(db_path):
    svc = ClinicalService(db_path)
    svc.ingest(payload("p-5", "durable"), "fhir-r4",
               request_id="req-restart", **ADMIN)
    svc.close()

    svc2 = ClinicalService(db_path)
    try:
        retry = svc2.ingest(payload("p-5", "durable"), "fhir-r4",
                            request_id="req-restart", **ADMIN)
        log.info("input=ingest(req-restart) after restart decision: %s", retry)
        assert retry == {"patient_id": "p-5", "version": 1}
        hist = svc2.history("p-5", **ADMIN)
        assert [v["version"] for v in hist["versions"]] == [1]
    finally:
        svc2.close()


def test_concurrent_writers_on_shared_file_then_restart(db_path):
    """Many threads write one file-backed service; after a restart the
    version chain must equal exactly the number of committed writes."""
    import threading

    from app.errors import ConcurrencyError

    svc = ClinicalService(db_path)
    svc.ingest(payload("p-conc", "base"), "fhir-r4", **ADMIN)
    wins, conflicts = [], []
    barrier = threading.Barrier(8)

    def worker(i: int):
        barrier.wait()
        try:
            current = svc.read("p-conc", **ADMIN)
            svc.update("p-conc", payload("p-conc", f"w{i}"), "fhir-r4",
                       expected_version=current["version"], **ADMIN)
            wins.append(i)
        except ConcurrencyError as e:
            conflicts.append(e.detail["expected_version"])

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    svc.close()
    log.info("input=8 concurrent writers (file-backed) decision: wins=%d "
             "conflicts=%d", len(wins), len(conflicts))
    assert len(wins) + len(conflicts) == 8

    # Reopen: every acknowledged write is visible, chain has no gaps.
    svc2 = ClinicalService(db_path)
    try:
        hist = svc2.history("p-conc", **ADMIN)
        versions = [v["version"] for v in hist["versions"]]
        log.info("decision after restart: history=%s expected_len=%d",
                 versions, 1 + len(wins))
        assert versions == list(range(1, 2 + len(wins)))
        current = svc2.read("p-conc", **ADMIN)
        assert current["version"] == 1 + len(wins)
    finally:
        svc2.close()
