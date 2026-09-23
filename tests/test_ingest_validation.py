"""版本不兼容、必填缺失、编码体系不可识别的拒绝归类。

要求：错误必须被正确归类且原因可区分；单测日志中打印输入、版本号与判定依据。
"""

from __future__ import annotations

import logging

import pytest

from app.errors import ErrorCategory, IngestError
from app.ingest import ingest_resource, normalize

VALID_V11 = {
    "resource_type": "Patient",
    "version": "1.1",
    "patient_id": "p-1",
    "name": "张三",
    "gender": "male",
    "birth_date": "1980-05-01",
    "active": True,
    "conditions": [
        {"system": "http://loinc.org", "code": "8302-2", "display": "身高"},
    ],
}


def test_normalize_v10_and_v11_accepted(store) -> None:
    p11 = normalize(dict(VALID_V11))
    assert p11.version == "1.1"
    assert p11.conditions[0].system == "http://loinc.org"

    v10 = {
        "resource_type": "Patient",
        "version": "1.0",
        "patient_id": "p-2",
        "name": "L. Name",
        "gender": "female",
        "birthDate": "1990-09-09",  # v1.0 使用 camelCase 别名
    }
    p10 = normalize(v10)
    assert p10.version == "1.0"
    assert p10.birth_date == "1990-09-09"
    assert p10.conditions == []


def test_unsupported_version_is_distinct_from_other_errors(store, caplog) -> None:
    for bad_payload, label in (
        ({**VALID_V11, "version": "2.0"}, "2.0"),
        ({k: v for k, v in VALID_V11.items() if k != "version"}, "<missing>"),
        ({**VALID_V11, "version": 1.1}, "<wrong-type>"),
    ):
        caplog.clear()
        with pytest.raises(IngestError) as exc_info:
            ingest_resource(store, bad_payload, actor="tester", role="clinician")
        err = exc_info.value
        # 归类：版本问题必须能与缺失字段、编码问题区分开
        assert err.category == ErrorCategory.UNSUPPORTED_VERSION or (
            label == "<wrong-type>" and err.category == ErrorCategory.MALFORMED_INPUT
        ), f"{label}: {err.category}"
        assert err.field == "version"
        # 原始线索保留：拒绝时不静默丢弃输入
        assert err.raw_hint is not None

        # 日志中必须出现输入键、版本号与判定依据（category）
        decision_logs = [
            r for r in caplog.records if r.name == "app.ingest" and r.levelno >= logging.WARNING
        ]
        assert decision_logs, f"{label}: 缺少判定日志"
        text = decision_logs[0].getMessage()
        assert "category=" in text
        assert "input_keys=" in text
        assert ("version=2.0" in text) or ("version=None" in text) or ("version=1.1" in text)

        # 归一化失败不得产生任何患者修订
        assert store.list_history_revs("p-1") == []


def test_missing_required_field_names_the_field(store, caplog) -> None:
    cases = [
        ({**VALID_V11, "name": "   "}, "name"),
        ({**VALID_V11, "gender": ""}, "gender"),
        ({k: v for k, v in VALID_V11.items() if k != "birth_date"}, "birth_date"),
    ]
    for bad_payload, logical_field in cases:
        caplog.clear()
        with pytest.raises(IngestError) as exc_info:
            normalize(bad_payload)
        err = exc_info.value
        assert err.category == ErrorCategory.MISSING_REQUIRED_FIELD
        assert err.field == logical_field
        assert logical_field in err.message
        # 日志打印版本号与输入，便于复盘判定依据
        record = next(r for r in caplog.records if r.name == "app.ingest")
        assert "version=1.1" in record.getMessage()
        assert "missing_required_field" in record.getMessage()


def test_unrecognized_coding_system_is_rejected_with_raw_hint(store, caplog) -> None:
    bad = {
        **VALID_V11,
        "conditions": [
            {"system": "http://internal.example.org/secret-codes", "code": "X-1"},
        ],
    }
    caplog.set_level(logging.DEBUG)
    with pytest.raises(IngestError) as exc_info:
        ingest_resource(store, bad, actor="tester", role="clinician")
    err = exc_info.value
    assert err.category == ErrorCategory.UNRECOGNIZED_CODING_SYSTEM
    assert err.field == "conditions[0].system"
    # 原始错误线索完整保留
    assert err.raw_hint["conditions"][0]["system"] == "http://internal.example.org/secret-codes"
    # 判定依据日志包含输入版本与被拒绝的体系
    text = " ".join(r.getMessage() for r in caplog.records if r.name == "app.ingest")
    assert "version=1.1" in text
    assert "http://internal.example.org/secret-codes" in text

    # 拒绝必须写审计，且审计中的 reason 与错误分类一致
    audits = store.read_audit("p-1")
    assert audits and audits[-1].decision == "deny"
    assert audits[-1].reason == "unrecognized_coding_system"


def test_malformed_inputs_are_not_silently_dropped() -> None:
    with pytest.raises(IngestError) as exc_info:
        normalize(["not", "an", "object"])  # type: ignore[arg-type]
    assert exc_info.value.category == ErrorCategory.MALFORMED_INPUT

    with pytest.raises(IngestError) as exc_info:
        normalize({**VALID_V11, "conditions": "not-a-list"})
    assert exc_info.value.category == ErrorCategory.MALFORMED_INPUT
