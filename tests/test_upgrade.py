"""Upgrade & recovery semantics for persistent local storage.

Covers:
  * opening a database written by the pre-history version of the service
    (only a ``resources`` table): history is backfilled so every prior write
    is listed in order with a timestamp and every version is retrievable;
  * the migration is durable and idempotent across restarts;
  * idempotent retries after a restart replay the recorded response instead
    of failing or writing twice;
  * a submission interrupted mid-flight commits nothing partially: neither
    the version bump nor the idempotency key survives, so the retry runs
    cleanly.
"""
from __future__ import annotations

import json
import logging
import sqlite3

import pytest

from app.errors import ErrorCode, NotFoundError
from app.models import PatientResource, Role
from app.service import ClinicalService
from app.storage import ResourceStore

log = logging.getLogger("test.upgrade")

ADMIN = {"actor": "alice", "role": Role.ADMIN}

# Schema of the pre-history release: current state only, no resource_history,
# no idempotency_keys, no audit table.
_LEGACY_SCHEMA = """
CREATE TABLE resources (
    patient_id TEXT PRIMARY KEY,
    version    INTEGER NOT NULL,
    updated_at REAL NOT NULL,
    body       TEXT NOT NULL
);
"""


def body(pid: str, mrn: str) -> dict:
    return {"patient_id": pid, "active": True,
            "identifiers": {"mrn": mrn},
            "observations": [{"code": {"system": "http://loinc.org",
                                       "code": "8480-6", "display": None},
                              "value": mrn, "unit": None, "effective": None}]}


def payload(pid: str, mrn: str) -> dict:
    return {"patient_id": pid, "identifiers": {"mrn": mrn},
            "observations": [{"code": {"system": "http://loinc.org",
                                       "code": "8480-6"}, "value": mrn}]}


def make_legacy_db(path: str, rows: list[tuple[str, int, float, dict]]) -> None:
    """Write a database exactly as the pre-history release would have."""
    conn = sqlite3.connect(path)
    conn.executescript(_LEGACY_SCHEMA)
    for pid, version, updated_at, b in rows:
        conn.execute("INSERT INTO resources VALUES (?,?,?,?)",
                     (pid, version, updated_at, json.dumps(b, sort_keys=True)))
    conn.commit()
    conn.close()
    log.info("legacy db written at %s: %s", path,
             [(pid, v) for pid, v, _, _ in rows])


def test_legacy_db_history_backfilled_on_upgrade(tmp_path):
    db = str(tmp_path / "legacy.db")
    # p-old accumulated 3 successful writes under the old version, p-one had 1.
    make_legacy_db(db, [("p-old", 3, 1700000003.0, body("p-old", "M3")),
                        ("p-one", 1, 1700000001.0, body("p-one", "only"))])

    svc = ClinicalService(db)  # upgrade: open the old file with the new code

    # Current content is exactly what the old version last wrote.
    current = svc.read("p-old", **ADMIN)
    log.info("upgraded read(p-old) -> version=%s mrn=%s",
             current["version"], current["identifiers"]["mrn"])
    assert current["version"] == 3
    assert current["identifiers"]["mrn"] == "M3"

    # Every prior successful write is now listed, in order, with timestamps.
    history = svc.history("p-old", **ADMIN)
    versions = [v["version"] for v in history["versions"]]
    stamps = [v["updated_at"] for v in history["versions"]]
    log.info("upgraded history(p-old) -> versions=%s updated_at=%s",
             versions, stamps)
    assert versions == [1, 2, 3]
    assert all(isinstance(ts, float) for ts in stamps)
    assert stamps == sorted(stamps)

    # ...and every version number is retrievable instead of VERSION_NOT_FOUND.
    for v in (1, 2, 3):
        snap = svc.read_version("p-old", v, **ADMIN)
        assert snap["version"] == v
    # The current version's snapshot is the true last-written content.
    assert svc.read_version("p-old", 3, **ADMIN)["identifiers"]["mrn"] == "M3"

    # Single-write patient: history of exactly one entry.
    assert [v["version"] for v in svc.history("p-one", **ADMIN)["versions"]] == [1]

    # Writes continue from the persisted version; new snapshots are real.
    resumed = svc.update("p-old", payload("p-old", "M4"), "fhir-r4",
                         expected_version=3, **ADMIN)
    log.info("post-upgrade update(p-old, expected=3) -> version=%s",
             resumed["version"])
    assert resumed["version"] == 4
    versions = [v["version"] for v in svc.history("p-old", **ADMIN)["versions"]]
    assert versions == [1, 2, 3, 4]
    assert svc.read_version("p-old", 4, **ADMIN)["identifiers"]["mrn"] == "M4"
    svc.close()


def test_backfilled_history_survives_restarts_and_is_not_duplicated(tmp_path):
    db = str(tmp_path / "legacy.db")
    make_legacy_db(db, [("p-old", 3, 1700000003.0, body("p-old", "M3"))])

    svc1 = ClinicalService(db)
    first = svc1.history("p-old", **ADMIN)["versions"]
    svc1.close()

    # Reopen twice: the migration must not duplicate or alter backfilled rows.
    svc2 = ClinicalService(db)
    second = svc2.history("p-old", **ADMIN)["versions"]
    svc2.close()
    svc3 = ClinicalService(db)
    third = svc3.history("p-old", **ADMIN)["versions"]
    log.info("history after 1st/2nd/3rd open: %s / %s / %s",
             [v["version"] for v in first], [v["version"] for v in second],
             [v["version"] for v in third])
    assert [v["version"] for v in second] == [1, 2, 3]
    assert second == first == third  # identical content + timestamps
    svc3.close()


def test_idempotent_retry_after_restart_replays_recorded_response(tmp_path):
    """A submission committed before a restart replays cleanly afterwards:
    same response, no new version, no duplicate success audit."""
    db = str(tmp_path / "clinical.db")
    svc = ClinicalService(db)
    svc.ingest(payload("p-r", "v1"), "fhir-r4", idempotency_key="boot-1", **ADMIN)
    svc.update("p-r", payload("p-r", "v2"), "fhir-r4", expected_version=1,
               idempotency_key="boot-2", **ADMIN)
    svc.close()

    # Service restarts; the client retries both submissions (e.g. it never
    # saw the responses because the network dropped during the restart).
    svc2 = ClinicalService(db)
    again_ingest = svc2.ingest(payload("p-r", "v1"), "fhir-r4",
                               idempotency_key="boot-1", **ADMIN)
    again_update = svc2.update("p-r", payload("p-r", "v2"), "fhir-r4",
                               expected_version=1, idempotency_key="boot-2",
                               **ADMIN)
    log.info("retried after restart: ingest -> %s, update -> %s",
             again_ingest, again_update)
    assert again_ingest == {"patient_id": "p-r", "version": 1}
    assert again_update == {"patient_id": "p-r", "version": 2}

    # No extra versions were written by the retries.
    assert svc2.read("p-r", **ADMIN)["version"] == 2
    assert [v["version"] for v in svc2.history("p-r", **ADMIN)["versions"]] == [1, 2]

    # One logical submission == one success audit entry, even across restart.
    writes = [a for a in svc2.audit_trail() if a.action in ("ingest", "update")]
    successes = [a for a in writes if a.outcome == "success"]
    replays = [a for a in writes if a.outcome == "replayed"]
    log.info("audit after restart retries: success=%d replayed=%d",
             len(successes), len(replays))
    assert len(successes) == 2   # the original ingest + update
    assert len(replays) == 2     # the two post-restart retries
    svc2.close()


def test_interrupted_submission_commits_nothing_partially(tmp_path):
    """Crash between the resource write and the key record: both roll back,
    so the retried submission neither errors nor writes twice."""
    db = str(tmp_path / "crash.db")
    store = ResourceStore(db)
    resource = PatientResource(patient_id="p-crash",
                               identifiers={"mrn": "M1"})

    def crashing_submission():
        store.create_tx(resource)
        raise RuntimeError("simulated crash before commit")

    with pytest.raises(RuntimeError):
        store.apply_idempotent("k-crash", "fp-1", crashing_submission)

    # Neither the version bump nor the idempotency key survived.
    with pytest.raises(NotFoundError) as exc:
        store.get("p-crash")
    log.info("after crash: get(p-crash) -> code=%s", exc.value.code)
    assert exc.value.code is ErrorCode.NOT_FOUND

    # The retried submission runs cleanly as a first-time write.
    response, replayed = store.apply_idempotent(
        "k-crash", "fp-1", lambda: store.create_tx(resource))
    log.info("retried after crash: response=%s replayed=%s", response, replayed)
    assert (response, replayed) == ({"patient_id": "p-crash", "version": 1}, False)
    store.close()

    # ...and the clean retry is durable across a restart.
    store2 = ResourceStore(db)
    assert store2.get("p-crash").version == 1
    response2, replayed2 = store2.apply_idempotent(
        "k-crash", "fp-1", lambda: store2.create_tx(resource))
    assert (response2, replayed2) == (response, True)  # replay, not rewrite
    assert store2.get("p-crash").version == 1
    store2.close()
