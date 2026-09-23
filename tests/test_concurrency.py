"""乐观并发控制测试。

覆盖：
* 并发更新只有一个成功，其余按 VERSION_CONFLICT 明确拒绝；
* 过期版本(409/VERSION_CONFLICT)与数据不存在(404/NOT_FOUND)原因可区分；
* 携带 request_id 的重试不产生重复写入、不丢更新；
* 串行多次重试最终收敛，所有修订都在历史中。
"""

from __future__ import annotations

import logging
import threading

import pytest

from app.errors import ErrorCategory, IngestError
from app.ingest import ingest_resource

PAYLOAD = {
    "resource_type": "Patient",
    "version": "1.1",
    "patient_id": "p-conc",
    "name": "初始",
    "gender": "male",
    "birth_date": "1975-03-03",
    "active": True,
    "conditions": [],
}


def _create(store, name="初始", request_id=None):
    return ingest_resource(
        store, {**PAYLOAD, "name": name},
        actor="t", role="clinician", request_id=request_id,
    )


def _update(store, rev, name):
    return ingest_resource(
        store, {**PAYLOAD, "name": name},
        actor="t", role="clinician", expected_rev=rev,
    )


def test_stale_revision_conflicts_with_distinct_category(store, caplog) -> None:
    created = _create(store)
    assert created.rev == 1
    _update(store, 1, "第二次")

    caplog.clear()
    # 持有过期 rev=1 的提交必须被明确拒绝
    with pytest.raises(IngestError) as conflict:
        _update(store, 1, "过期提交")
    assert conflict.value.category == ErrorCategory.VERSION_CONFLICT
    assert "expected_rev=1" in conflict.value.message
    assert "当前 rev=2" in conflict.value.message

    # 数据不存在是另一类错误：NOT_FOUND，而不是 VERSION_CONFLICT
    with pytest.raises(IngestError) as missing:
        _update_with_id(store, "does-not-exist", 1, "x")
    assert missing.value.category == ErrorCategory.NOT_FOUND
    assert conflict.value.category != missing.value.category

    # 判定依据日志：版本号、当前 rev、期望 rev
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "version=1.1" in text
    assert "version_conflict" in text


def _update_with_id(store, patient_id, expected_rev, name):
    return ingest_resource(
        store, {**PAYLOAD, "patient_id": patient_id, "name": name},
        actor="t", role="clinician", expected_rev=expected_rev,
    )


def test_concurrent_updates_one_wins_no_lost_revision(store) -> None:
    created = _create(store)
    assert created.rev == 1

    barrier = threading.Barrier(8)
    results: dict[int, object] = {}
    lock = threading.Lock()

    def worker(idx: int) -> None:
        barrier.wait()  # 尽可能让所有线程同时提交
        try:
            patient = _update(store, 1, f"worker-{idx}")
            outcome: object = ("ok", patient.rev, patient.name)
        except IngestError as exc:
            outcome = ("conflict", exc.category)
        with lock:
            results[idx] = outcome

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    oks = [v for v in results.values() if v[0] == "ok"]
    conflicts = [v for v in results.values() if v[0] == "conflict"]
    # 同一基准 rev=1 上，恰有一个成功，其余全部明确冲突
    assert len(oks) == 1, results
    assert len(conflicts) == 7, results
    assert all(v[1] == ErrorCategory.VERSION_CONFLICT for v in conflicts)
    # 胜出者产生 rev=2，未产生重复修订
    assert oks[0][1] == 2
    assert store.list_history_revs("p-conc") == [1, 2]
    assert store.get_patient("p-conc").name == oks[0][2]


def test_retries_with_request_id_are_idempotent(store) -> None:
    first = _create(store, name="首次写入", request_id="idem-001")
    assert first.rev == 1

    # 同一 request_id 多次“重试”（即使内容相同）返回同一结果，不重复写入
    for _ in range(5):
        again = ingest_resource(
            store, {**PAYLOAD, "name": "首次写入"},
            actor="t", role="clinician", request_id="idem-001",
        )
        assert again.rev == first.rev
    assert store.list_history_revs("p-conc") == [1]

    # 不同 request_id 的新建仍是冲突而不是静默覆盖
    with pytest.raises(IngestError) as exc:
        ingest_resource(
            store, {**PAYLOAD, "name": "另一个请求"},
            actor="t", role="clinician", request_id="idem-002",
        )
    assert exc.value.category == ErrorCategory.VERSION_CONFLICT
    assert store.list_history_revs("p-conc") == [1]  # 冲突不产生修订


def test_serial_retry_loop_converges_without_lost_updates(store) -> None:
    _create(store)

    def read_modify_write(idx: int) -> int:
        """读-改-写，遇到冲突就基于最新 rev 重试。"""
        for _attempt in range(20):
            current = store.get_patient("p-conc")
            assert current is not None
            try:
                saved = _update(store, current.rev, f"name-{idx}-rev{current.rev + 1}")
                return saved.rev
            except IngestError as exc:
                assert exc.category == ErrorCategory.VERSION_CONFLICT
        raise AssertionError("重试未收敛")  # pragma: no cover

    for i in range(1, 6):
        new_rev = read_modify_write(i)
        assert new_rev == i + 1

    # 每次成功更新各产生一个不可变历史修订：无丢失、无重复
    assert store.list_history_revs("p-conc") == [1, 2, 3, 4, 5, 6]
    assert store.get_patient("p-conc").rev == 6


def test_conflict_and_not_found_are_audited(store) -> None:
    _create(store)
    with pytest.raises(IngestError):
        _update(store, 99, "过期")
    with pytest.raises(IngestError):
        _update_with_id(store, "missing-id", 1, "x")

    entries = store.read_audit("p-conc")
    deny_reasons = {e.reason for e in entries if e.decision == "deny"}
    assert deny_reasons == {"version_conflict"}

    missing_entries = store.read_audit("missing-id")
    assert any(e.reason == "not_found" and e.decision == "deny" for e in missing_entries)
