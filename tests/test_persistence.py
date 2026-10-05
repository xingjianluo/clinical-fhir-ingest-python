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


def test_concurrent_writes_on_persistent_store_then_restart(tmp_path):
    """Many clients race on one patient of a file-backed service: only
    latest-version commits win, stale ones are rejected with the deciding
    versions in the detail, and every committed write is readable afterwards
    — including after a restart."""
    import threading

    from app.errors import ConcurrencyError

    db = str(tmp_path / "race.db")
    svc = ClinicalService(db)
    svc.ingest(payload("p-c", "base"), "fhir-r4", **ADMIN)

    wins, conflicts = [], []
    barrier = threading.Barrier(8)

    def worker(i: int):
        barrier.wait()
        try:
            current = svc.read("p-c", **ADMIN)
            svc.update("p-c", payload("p-c", f"w{i}"), "fhir-r4",
                       expected_version=current["version"], **ADMIN)
            wins.append(i)
        except ConcurrencyError as e:
            conflicts.append(e.detail)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    final = svc.read("p-c", **ADMIN)
    log.info("8 concurrent writers decision: wins=%d conflicts=%d "
             "final_version=%s", len(wins), len(conflicts), final["version"])
    assert len(wins) + len(conflicts) == 8
    assert len(wins) >= 1
    # every rejection names the deciding versions: stale expected vs current
    for detail in conflicts:
        assert detail["expected_version"] < detail["current_version"]
    # version advanced exactly once per accepted write
    assert final["version"] == 1 + len(wins)

    # every committed write is retrievable by version, in order, with times
    history = svc.history("p-c", **ADMIN)["versions"]
    assert [v["version"] for v in history] == list(range(1, final["version"] + 1))
    stamps = [v["updated_at"] for v in history]
    assert stamps == sorted(stamps)
    snapshots = {version: svc.read_version("p-c", version, **ADMIN)
                 for version in range(1, final["version"] + 1)}
    svc.close()

    # After a restart the committed state is bit-for-bit identical.
    svc2 = ClinicalService(db)
    assert svc2.read("p-c", **ADMIN) == final
    assert svc2.history("p-c", **ADMIN)["versions"] == history
    for version, snap in snapshots.items():
        assert svc2.read_version("p-c", version, **ADMIN) == snap
    log.info("post-restart: current, %d history entries and %d snapshots "
             "all identical", len(history), len(snapshots))
    svc2.close()
