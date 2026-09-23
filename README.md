# clinical-fhir-ingest

面向临床数据互操作的资源摄取与受控访问服务。接收多种版本的临床资源表示，
归一到统一内部模型，以 SQLite 本地存储患者数据与审计记录，并提供带版本的
乐观并发控制、按角色的访问约束与去标识化导出。

## 架构

```
main.py            FastAPI 路由与错误码 -> HTTP 状态映射
app/normalize.py   版本适配器：fhir-r4 / fhir-r5 -> 统一内部模型
app/models.py      内部模型（PatientResource / StoredResource / AuditRecord）
app/storage.py     SQLite 存储 + 乐观并发控制（compare-and-swap）
app/access.py      角色访问控制 + 去标识化导出视图
app/audit.py       追加式审计日志（不经任何读取接口暴露）
app/service.py     编排：授权 -> 归一化 -> 存储 -> 审计
app/errors.py      可区分的错误分类（ErrorCode）
```

## 版本兼容范围

| 外部版本   | 支持 | 说明 |
|-----------|------|------|
| `fhir-r4` | ✅   | `identifiers` 为平铺对象，观察值取 `value` |
| `fhir-r5` | ✅   | `identifier` 为 `{type, value}` 列表，观察值取 `valueString` 或 `valueQuantity` |
| 其他      | ❌   | 拒绝，返回 `unsupported_version` |

可识别的编码体系：`http://loinc.org`、`http://snomed.info/sct`、
`http://hl7.org/fhir/sid/icd-10`。

摄取拒绝原因均可区分，且原始错误线索保留在 `detail` 中：

| ErrorCode | HTTP | 触发条件 |
|---|---|---|
| `missing_required_field` | 422 | 必填字段缺失（如 `patient_id`、`mrn`、观察值） |
| `unknown_coding_system`  | 422 | 编码体系不在白名单 |
| `unsupported_version`    | 422 | 外部版本不受支持 |
| `malformed_payload`      | 400 | 载荷结构非法 |

## 并发控制语义

- 每个资源携带单调递增的 `version`（创建时为 1）。
- 更新必须携带 `expected_version`，存储层执行
  `UPDATE ... WHERE patient_id=? AND version=?` 的比较-交换。
- 过期版本提交返回 **409 `version_conflict`**，并在 `detail` 中给出
  `expected_version` 与 `current_version`；资源不存在返回
  **404 `not_found`**，两种原因严格可区分。
- 冲突后的安全重试模式：重新读取当前版本 → 以新版本号重提交。
  重试不会产生重复写入，也不会丢失更新（版本号恰好等于
  `1 + 成功写入次数`，由并发测试验证）。

## 访问控制与去标识化

调用方身份通过请求头提供（演示级认证）：`X-Actor`、`X-Role`。

| 操作 | admin | clinician | researcher |
|---|---|---|---|
| 摄取 / 更新 | ✅ | ✅ | ❌ 403 `forbidden` |
| 标识化读取 `GET /patients/{id}` | ✅ | ✅ | ❌ 403 `forbidden` |
| 去标识化导出 `GET /patients/{id}/export` | ✅ | ✅ | ✅ |

- 越权读取不返回任何敏感字段，只返回错误对象。
- 导出视图移除全部直接标识（`name`、`birth_date`、`mrn`、`address`、
  `phone`、`email` 及整个 `identifiers`），患者引用替换为
  `anon-<sha256 前 12 位>` 的确定性假名；临床内容（观察编码、值、单位）保留。
  出口处还有 `assert_deidentified` 纵深防御校验。
- 每次操作尝试（成功或被拒）都会追加审计记录（操作者、角色、动作、
  结果、判定依据）。审计记录**不通过任何 HTTP 接口暴露**，仅可经
  `ClinicalService.audit_trail()` 在进程内做取证访问。

## API 摘要

```
POST /patients?source_version=fhir-r4            摄取（201）
PUT  /patients/{id}?source_version=...&expected_version=N   更新
GET  /patients/{id}                              标识化读取
GET  /patients/{id}/export                       去标识化导出
```

错误响应统一为 `{"error": {"code", "message", "detail"}}`。

## 本地验证

```bash
uv sync                                     # 安装依赖
uv run pytest -q --log-cli-level=INFO       # 运行测试，日志打印输入/版本号/判定依据
CLINICAL_DB=./data/clinical.db uv run uvicorn main:app --reload   # 启动服务
```

示例：

```bash
curl -X POST 'http://127.0.0.1:8000/patients?source_version=fhir-r4' \
  -H 'X-Actor: alice' -H 'X-Role: clinician' -H 'Content-Type: application/json' \
  -d '{"patient_id":"p1","identifiers":{"mrn":"M1"},
       "observations":[{"code":{"system":"http://loinc.org","code":"8480-6"},"value":"120"}]}'

curl -H 'X-Actor: bob' -H 'X-Role: researcher' \
  http://127.0.0.1:8000/patients/p1/export    # 去标识化视图
```

测试覆盖：并发冲突（过期版本 vs 不存在的区分、重试安全性、多写者竞争）、
版本不兼容（三类拒绝原因可区分）、越权读取与去标识化导出、审计完整性。
