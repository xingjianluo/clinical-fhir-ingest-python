"""Upgrade compatibility: databases written by the pre-history build of this
service (only a `resources` table, no `resource_history`/`idempotency_keys`)
must keep working after the upgrade — current state readable, version history
listed in order with write timestamps, snapshots retrievable by version, and
new writes continuing the version sequence across restarts."""
from __future__ import annotations

import json
import logging
import sqlite3

import pytest

from app.errors import ErrorCode, VersionNotFoundError
from app.models import Role
from app.service import ClinicalService

log = logging.getLogger("test.legacy_upgrade")

ADMIN = {"actor": "alice", "role": Role.ADMIN}

# Schema of the pre-history build (commit "实现临床资源摄取与受控访问流程"):
# a single `resources` table, no resource_history, no idempotency_keys.
_LEGACY_SCHEMA = """
CREATE TABLE resources (
    patient_id TEXT PRIMARY KEY,
    version    INTEGER NOT NULL,
    updated_at REAL NOT NULL,
    body       TEXT NOT NULL
);
"""


def _body(pid: str, mrn: str) -> str:
    return json.dumps({
        "patient_id": pid,
        "active": True,
        "identifiers": {"mrn": mrn},
        "observations": [{"code": {"system": "http://loinc.org",
                                   "code": "8480-6", "display": None},
                          "value": mrn, "unit": None, "effective": None}],
    }, sort_keys=True)


def make_legacy_db(path: str) -> None:
    """Simulate data accumulated by the old build: p-old was created and then
    updated twice (current version 3), p-single only ever created (v1)."""
    conn = sqlite3.connect(path)
    with conn:
        conn.executescript(_LEGACY_SCHEMA)
        conn.execute("INSERT INTO resources VALUES (?,?,?,?)",
                     ("p-old", 3, 1700000003.0, _body("p-old", "mrn-v3")))
        conn.execute("INSERT INTO resources VALUES (?,?,?,?)",
                     ("p-single", 1, 1700000001.0, _body("p-single", "mrn-only")))
    conn.close()


def payload(pid: str, mrn: str) -> dict:
    return {"patient_id": pid, "identifiers": {"mrn": mrn},
            "observations": [{"code": {"system": "http://loinc.org",
                                       "code": "8480-6"}, "value": mrn}]}


def test_legacy_db_history_and_version_readback_after_upgrade(tmp_path):
    db = str(tmp_path / "legacy.db")
    make_legacy_db(db)
    log.info("legacy data: p-old at v3, p-single at v1 (no history table)")

    svc = ClinicalService(db)  # upgrade: open the old file with the new build

    # Current content still readable, unchanged by the migration.
    current = svc.read("p-old", **ADMIN)
    log.info("post-upgrade read(p-old) -> version=%s mrn=%s",
             current["version"], current["identifiers"]["mrn"])
    assert current["version"] == 3
    assert current["identifiers"]["mrn"] == "mrn-v3"

    # History is no longer empty: every recoverable committed version is
    # listed in order with its write timestamp.
    history = svc.history("p-old", **ADMIN)["versions"]
    log.info("post-upgrade history(p-old) -> versions=%s updated_at=%s",
             [v["version"] for v in history],
             [v["updated_at"] for v in history])
    assert [v["version"] for v in history] == [3]
    assert history[0]["updated_at"] == 1700000003.0
    assert history[0]["identifiers"]["mrn"] == "mrn-v3"

    # The surviving snapshot is retrievable by its version number.
    snap = svc.read_version("p-old", 3, **ADMIN)
    assert snap["identifiers"]["mrn"] == "mrn-v3"
    assert snap["updated_at"] == 1700000003.0

    # Versions the old build never recorded are honestly reported as
    # VERSION_NOT_FOUND (distinct code + detail), not silently fabricated.
    with pytest.raises(VersionNotFoundError) as exc:
        svc.read_version("p-old", 1, **ADMIN)
    log.info("read_version(p-old, 1) on legacy data decision: code=%s detail=%s",
             exc.value.code, exc.value.detail)
    assert exc.value.code is ErrorCode.VERSION_NOT_FOUND
    assert exc.value.detail["version"] == 1

    # Single-write legacy patient: its only version is fully recoverable.
    single = svc.history("p-single", **ADMIN)["versions"]
    assert [v["version"] for v in single] == [1]
    assert svc.read_version("p-single", 1, **ADMIN)["identifiers"]["mrn"] == "mrn-only"

    # New writes continue the persisted version sequence and are appended
    # to the recovered history in the same order.
    resumed = svc.update("p-old", payload("p-old", "mrn-v4"), "fhir-r4",
                         expected_version=3, **ADMIN)
    log.info("post-upgrade update(p-old, expected=3) -> version=%s",
             resumed["version"])
    assert resumed["version"] == 4
    versions = svc.history("p-old", **ADMIN)["versions"]
    assert [v["version"] for v in versions] == [3, 4]
    assert svc.read_version("p-old", 4, **ADMIN)["identifiers"]["mrn"] == "mrn-v4"
    svc.close()

    # Restart on the migrated file: current state, recovered history and the
    # post-upgrade write are all still there; migration is idempotent
    # (reopening must not duplicate history rows).
    svc2 = ClinicalService(db)
    again = svc2.history("p-old", **ADMIN)["versions"]
    log.info("post-restart history(p-old) -> versions=%s",
             [v["version"] for v in again])
    assert [v["version"] for v in again] == [3, 4]
    assert svc2.read("p-old", **ADMIN)["identifiers"]["mrn"] == "mrn-v4"
    svc2.close()


def test_legacy_db_gains_durable_idempotency_after_upgrade(tmp_path):
    """The old build had no idempotency_keys table either. After the upgrade,
    keyed submissions must be recorded durably: a retry that arrives after a
    restart replays the original result instead of failing or rewriting."""
    db = str(tmp_path / "legacy-idem.db")
    make_legacy_db(db)
    key = "legacy-submit-1"

    svc = ClinicalService(db)
    first = svc.ingest(payload("p-new", "keyed"), "fhir-r4",
                       idempotency_key=key, **ADMIN)
    log.info("post-upgrade keyed ingest -> %s", first)
    svc.close()

    svc2 = ClinicalService(db)  # restart before the client's retry
    second = svc2.ingest(payload("p-new", "keyed"), "fhir-r4",
                         idempotency_key=key, **ADMIN)
    log.info("retry after restart -> %s", second)
    assert first == {"patient_id": "p-new", "version": 1}
    assert second == first
    assert svc2.read("p-new", **ADMIN)["version"] == 1
    successes = [a for a in svc2.audit_trail()
                 if a.action == "ingest" and a.outcome == "success"]
    assert len(successes) == 1
    svc2.close()
