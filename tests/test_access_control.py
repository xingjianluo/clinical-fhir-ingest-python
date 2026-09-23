"""越权读取、去标识化导出与审计隔离测试（含 HTTP 层）。"""

from __future__ import annotations

from app import access
from app.errors import ErrorCategory, IngestError
from app.ingest import ingest_resource
from app.models import Coding, Patient

PAYLOAD = {
    "resource_type": "Patient",
    "version": "1.1",
    "patient_id": "p-acl",
    "name": "李四保密",
    "gender": "female",
    "birth_date": "1992-11-30",
    "active": True,
    "conditions": [
        {"system": "http://snomed.info/sct", "code": "73211009", "display": "糖尿病"},
    ],
}


def _seed(store):
    return ingest_resource(store, PAYLOAD, actor="doc", role="clinician")


# ---- 单元层：投影与去标识化 -------------------------------------------------

def test_analyst_view_contains_no_direct_identifiers(store) -> None:
    patient = _seed(store)
    view = access.project_for_read(patient, "analyst")

    for sensitive in ("patient_id", "name", "request_id", "created_at", "updated_at"):
        assert sensitive not in view, f"分析师视图泄漏敏感字段: {sensitive}"
    # 仅保留分析所需的粗粒度字段
    assert view["birth_year"] == "1992"
    assert view["age_band"] == "30-39"
    assert view["gender"] == "female"
    # 编码不携带可能含个人叙述的 display 文本
    assert view["conditions"] == [{"system": "http://snomed.info/sct", "code": "73211009"}]


def test_unknown_role_gets_empty_projection() -> None:
    patient = Patient(
        patient_id="x", resource_type="Patient", version="1.0", rev=1,
        name="密", gender="male", birth_date="2000-01-01", active=True,
        conditions=[Coding("http://loinc.org", "1")],
    )
    assert access.project_for_read(patient, "attacker") == {}
    assert access.can_write("attacker") is False
    assert access.can_read_audit("attacker") is False
    assert access.can_export_deidentified("attacker") is False


def test_role_matrix() -> None:
    assert access.can_read_full("admin") and access.can_read_full("clinician")
    assert not access.can_read_full("analyst")
    assert access.can_write("clinician") and not access.can_write("analyst")
    assert access.can_read_audit("admin")
    assert not access.can_read_audit("clinician") and not access.can_read_audit("analyst")
    assert access.can_export_deidentified("analyst") and not access.can_export_deidentified("clinician")


# ---- HTTP 层 ---------------------------------------------------------------

def test_full_read_for_clinician_but_not_analyst_identifiers(client) -> None:
    r = client.post("/resources/Patient", json=PAYLOAD, headers={"X-Actor": "d", "X-Role": "clinician"})
    assert r.status_code == 200

    full = client.get("/resources/Patient/p-acl", headers={"X-Role": "clinician"})
    assert full.status_code == 200 and full.json()["name"] == "李四保密"

    deid = client.get("/resources/Patient/p-acl", headers={"X-Role": "analyst"})
    assert deid.status_code == 200
    body = deid.json()
    assert "name" not in body and "patient_id" not in body
    assert body["age_band"] == "30-39"


def test_unauthorized_roles_are_forbidden_and_leak_nothing(client) -> None:
    client.post("/resources/Patient", json=PAYLOAD, headers={"X-Role": "clinician"})

    for role in ("attacker", "", "auditor"):
        headers = {"X-Role": role} if role else {}
        r = client.get("/resources/Patient/p-acl", headers=headers)
        assert r.status_code == 403, f"role={role!r}"
        assert r.json()["category"] == ErrorCategory.FORBIDDEN.value
        # 错误响应体中不得夹带患者字段
        assert "李四保密" not in r.text
        assert "1992-11-30" not in r.text

    # 无权限角色不能写
    r = client.post("/resources/Patient", json=PAYLOAD, headers={"X-Role": "analyst"})
    assert r.status_code == 403
    # 不能看历史修订列表
    r = client.get("/resources/Patient/p-acl/revisions", headers={"X-Role": "analyst"})
    assert r.status_code == 403
    # clinician 也不能导出（导出是 analyst/admin 的去标识化通道）
    r = client.get("/resources/Patient/p-acl/export", headers={"X-Role": "clinician"})
    assert r.status_code == 403


def test_export_endpoint_is_deidentified(client) -> None:
    client.post("/resources/Patient", json=PAYLOAD, headers={"X-Role": "clinician"})
    r = client.get("/resources/Patient/p-acl/export", headers={"X-Actor": "bi", "X-Role": "analyst"})
    assert r.status_code == 200
    doc = r.json()
    assert set(doc) == {"resource_type", "gender", "birth_year", "age_band", "active", "conditions"}
    assert doc["birth_year"] == "1992"


def test_audit_channel_isolated_from_normal_reads(client) -> None:
    client.post("/resources/Patient", json=PAYLOAD, headers={"X-Actor": "d", "X-Role": "clinician"})
    client.get("/resources/Patient/p-acl", headers={"X-Role": "clinician"})
    client.get("/resources/Patient/p-acl", headers={"X-Role": "analyst"})

    # 普通读取接口拿不到任何审计内容
    normal = client.get("/resources/Patient/p-acl", headers={"X-Role": "admin"})
    assert "audit" not in normal.json() and "audit_id" not in normal.text

    # clinician / analyst 无法读取审计通道
    assert client.get("/audit/entries", headers={"X-Role": "clinician"}).status_code == 403
    assert client.get("/audit/entries", headers={"X-Role": "analyst"}).status_code == 403

    # 仅 admin 可读取，且记录完整（含越权尝试）
    client.get("/resources/Patient/p-acl", headers={"X-Role": "forged"})  # 触发越权读
    r = client.get("/audit/entries", headers={"X-Actor": "root", "X-Role": "admin"})
    assert r.status_code == 200
    entries = r.json()["entries"]
    assert any(e["decision"] == "allow" and e["action"] == "create" for e in entries)
    assert any(e["decision"] == "allow" and e["reason"] == "full" for e in entries)
    assert any(e["decision"] == "allow" and e["reason"] == "deidentified" for e in entries)
    denied_reads = [e for e in entries if e["action"] == "read" and e["decision"] == "deny"]
    assert denied_reads and all(e["reason"] == "forbidden" for e in denied_reads)
    # 每条审计都具备完整要素
    for e in entries:
        assert e["audit_id"] and e["timestamp"] and e["actor"] is not None
        assert e["decision"] in {"allow", "deny"}


def test_audit_filter_by_patient_and_not_found_distinction(client) -> None:
    client.post("/resources/Patient", json=PAYLOAD, headers={"X-Role": "clinician"})
    # 读取不存在的资源 -> 404 而不是 403/409
    r = client.get("/resources/Patient/nope", headers={"X-Role": "clinician"})
    assert r.status_code == 404 and r.json()["category"] == "not_found"

    r = client.get("/audit/entries?patient_id=p-acl", headers={"X-Role": "admin"})
    assert r.status_code == 200
    assert all(e["patient_id"] == "p-acl" for e in r.json()["entries"])
