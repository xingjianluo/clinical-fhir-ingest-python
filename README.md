# clinical-fhir-ingest

面向临床数据互操作的资源摄取与受控访问服务，可作为常驻的本地互操作服务运行：
多个脚本/终端可同时写入，服务可随时重启。接收多种版本的临床资源表示，
归一到统一内部模型，以 SQLite 本地持久化患者数据、版本历史与审计记录，
并提供带版本的乐观并发控制、幂等重试、按角色的访问约束与去标识化导出。

## 架构

```
main.py            FastAPI 路由与错误码 -> HTTP 状态映射
app/normalize.py   版本适配器：fhir-r4 / fhir-r5 -> 统一内部模型
app/models.py      内部模型（PatientResource / StoredResource / VersionEntry / AuditRecord）
app/storage.py     SQLite 存储：当前状态 + 追加式版本历史 + 幂等账本（乐观并发控制）
app/access.py      角色访问控制 + 去标识化导出视图
app/audit.py       追加式审计日志（不经任何读取接口暴露）
app/service.py     编排：授权 -> 归一化 -> 存储 -> 审计
app/errors.py      可区分的错误分类（ErrorCode）
```

## 常驻本地存储与重启恢复语义

设置 `CLINICAL_DB=<路径>` 后，所有成功接收的数据都落在同一个 SQLite 文件中
（WAL 模式），包含三张表：

| 表 | 内容 |
|---|---|
| `resources` | 每个患者的当前状态与当前版本号 |
| `resource_history` | 每次成功写入的完整快照（追加式，按 `(patient_id, version)` 主键） |
| `requests` | 幂等账本：`request_id -> 已记录结果` |

- **单一份当前状态**：无论并发写入还是稍后读取，看到的都是同一份已提交状态；
  写路径由进程内可重入锁 + SQLite 事务串行化，不会丢写也不会重复应用。
- **重启恢复**：已提交的写入在进程重启后仍然可见；重新打开同一数据库文件后，
  当前状态、每个患者的完整版本链和审计记录都在，版本号从重启前的位置继续递增，
  不会断链或回退。
- **升级兼容**：历史表与幂等账本按 `CREATE TABLE IF NOT EXISTS` 建立；对历史版本
  （无 `resource_history` 表）写入的旧库，启动时会用当前状态回填一条历史记录，
  保证版本链完整。
- **保留规则**：版本历史只增不删——每次成功写入追加一条快照，不做压缩或清理，
  任何历史版本都可按版本号取回。

## 版本历史 API

```
GET /patients/{id}/history            列出版本顺序与写入时间（仅元数据，不含标识信息）
GET /patients/{id}/history/{version}  取回指定版本的快照
```

- 历史列表对所有具读取权的角色开放（内容只有版本号与时间戳）。
- 快照读取按角色控制：admin / clinician 看到标识化视图；researcher 只能拿到
  该历史版本的**去标识化**视图（与导出同一套脱敏与纵深防御校验）。
- 三类失败严格可区分：

| 情形 | HTTP | ErrorCode |
|---|---|---|
| 患者不存在 | 404 | `not_found` |
| 患者存在但版本不存在 | 404 | `version_not_found`（`detail` 给出有效版本范围） |
| 角色无任何读取权 | 403 | `forbidden` |

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
- **并发提交判定规则**：多客户端同时更新同一患者时，只有基于当前最新版本的
  提交能成功；过期提交返回 **409 `version_conflict`**，`detail` 中给出
  `expected_version` 与 `current_version` 作为判定依据；资源不存在返回
  **404 `not_found`**，两种原因严格可区分。
- **已确认写入的可见性**：凡是成功返回的写入，后续任何一次读取都能查到，
  重启后依旧一致（版本号恰好等于 `1 + 成功写入次数`，由并发测试验证）。
- **幂等重试**：写入请求可携带 `Idempotency-Key` 请求头。提交与账本记录在同一
  事务中落库，因此同一次提交因超时/网络问题以相同 key 重试时，直接返回首次
  记录的结果——不会凭空多出版本，审计也不会记重。冲突后的安全重试模式仍是：
  重新读取当前版本 → 以新版本号（和新 key）重提交。

## 访问控制与去标识化

调用方身份通过请求头提供（演示级认证）：`X-Actor`、`X-Role`。

| 操作 | admin | clinician | researcher |
|---|---|---|---|
| 摄取 / 更新 | ✅ | ✅ | ❌ 403 `forbidden` |
| 标识化读取 `GET /patients/{id}` | ✅ | ✅ | ❌ 403 `forbidden` |
| 去标识化导出 `GET /patients/{id}/export` | ✅ | ✅ | ✅ |
| 版本历史列表 `GET /patients/{id}/history` | ✅ | ✅ | ✅（仅元数据） |
| 历史快照 `GET /patients/{id}/history/{v}` | ✅ 标识化 | ✅ 标识化 | ✅ 去标识化 |

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
POST /patients?source_version=fhir-r4            摄取（201，可带 Idempotency-Key）
PUT  /patients/{id}?source_version=...&expected_version=N   更新（可带 Idempotency-Key）
GET  /patients/{id}                              标识化读取
GET  /patients/{id}/export                       去标识化导出
GET  /patients/{id}/history                      版本历史列表
GET  /patients/{id}/history/{version}            指定版本快照（按角色标识化/去标识化）
```

错误响应统一为 `{"error": {"code", "message", "detail"}}`。

## 本地验证

```bash
uv sync                                     # 安装依赖
uv run pytest -q --log-cli-level=INFO       # 运行测试，日志打印输入/版本号/判定依据
CLINICAL_DB=./data/clinical.db uv run uvicorn main:app --reload   # 启动常驻服务
```

示例：

```bash
curl -X POST 'http://127.0.0.1:8000/patients?source_version=fhir-r4' \
  -H 'X-Actor: alice' -H 'X-Role: clinician' -H 'Idempotency-Key: req-001' \
  -H 'Content-Type: application/json' \
  -d '{"patient_id":"p1","identifiers":{"mrn":"M1"},
       "observations":[{"code":{"system":"http://loinc.org","code":"8480-6"},"value":"120"}]}'

curl -H 'X-Actor: bob' -H 'X-Role: researcher' \
  http://127.0.0.1:8000/patients/p1/export          # 去标识化视图

curl -H 'X-Actor: alice' -H 'X-Role: clinician' \
  http://127.0.0.1:8000/patients/p1/history         # 版本历史
curl -H 'X-Actor: alice' -H 'X-Role: clinician' \
  http://127.0.0.1:8000/patients/p1/history/1       # v1 快照
```

复现重启恢复：以 `CLINICAL_DB=./data/clinical.db` 启动服务并写入若干版本，
`Ctrl-C` 停止后用同一命令重启——当前状态、版本历史与审计记录均在，
继续更新时版本号从重启前递增。复现幂等重试：用同一 `Idempotency-Key`
重复提交同一写入，两次返回相同结果且版本号只前进一次。

测试覆盖：并发冲突（过期版本 vs 不存在的区分、重试安全性、多写者竞争）、
重启后数据/版本链/审计的持久性、按版本号回溯读取、幂等重试不多版本不重复审计、
版本不兼容（三类拒绝原因可区分）、越权读取与去标识化导出、历史接口的角色视图
与三类失败区分、审计完整性。
