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
                   + 不可变版本历史（resource_history）+ 幂等键记录
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

### 幂等提交（防重试重复）

`POST /patients` 与 `PUT /patients/{id}` 接受可选请求头 `Idempotency-Key`：

- 同一 key + 同一请求内容的重试（超时、网络抖动后的重发）返回**首次提交
  记录的结果**，不产生新版本；审计中首次提交记 `success`，重放记
  `replayed`，同一逻辑提交永远只有一条 `success`。
- 同一 key 携带**不同**请求内容会被拒绝（`malformed_payload`），防止
  key 误用被静默吞掉。
- 幂等记录与资源数据存在同一个 SQLite 文件中，**重启后仍然有效**；
  并发携带同一 key 的提交被串行化，恰好只有一个真正落库。

## 常驻本地存储与重启恢复

设置 `CLINICAL_DB=/path/to/clinical.db` 后，资源当前状态、逐版本历史、
幂等记录与审计日志全部落在同一个 SQLite 文件（WAL 模式）中：

- **单一日志源**：所有客户端（脚本、终端、并发连接）读写的是同一份
  磁盘状态；任何一次成功提交在提交返回前已落库，之后任何读取（包括
  重启后的读取）都能看到。
- **重启语义**：进程重启后用同一个 `CLINICAL_DB` 路径启动即可恢复——
  当前状态、每个患者的全部历史版本、审计记录、幂等 key 都在；版本号
  从持久化的当前值继续递增，不会回退或断档。
- **历史保留规则**：每次成功写入（创建或更新）在与当前状态相同的
  事务里追加一行不可变历史快照（`resource_history` 表），当前状态与
  历史不可能分叉；历史只增不删，版本号在该患者生命周期内唯一且单调。
- **兼容规则**：历史快照按写入时的归一化内部模型存储，读取时按当前
  模型反序列化；旧版本快照永远保持写入时的内容（不可变），不会被
  后续更新改写。
- **老数据升级（无历史表的库）**：更早版本的服务只写 `resources` 当前行，
  没有 `resource_history` / `idempotency_keys` 表。用当前版本直接打开
  这种老库时，启动迁移会（幂等地）为每个患者把**当前版本**的快照回填进
  历史表：历史不再为空、当前版本可按版本号取回、写入时间沿用原行的
  `updated_at`，后续写入从当前版本号继续递增并正常追加历史。老版本
  服务当年没有记录更早的中间版本，这些内容无法凭空捏造——按那些版本号
  读取会明确返回 `version_not_found`（`detail` 含请求的版本号），而不是
  伪造内容。迁移是 `INSERT OR IGNORE`，重复打开不会重复回填。
- **写入与幂等记录的原子性**：带 `Idempotency-Key` 的提交把"资源写入 +
  历史快照 + 幂等记录"放在**同一个事务**里提交。服务在提交途中崩溃或
  重启只会留下"都落库"或"都没落库"两种状态：前者让重启后的重试直接
  重放首次记录的结果，后者让重试干净地重新执行这次写入——不会出现
  写了一半导致重试报错的中间态。

## 版本历史读取

```
GET /patients/{id}/history          按提交顺序列出全部版本（含写入时间）
GET /patients/{id}/versions/{n}     取回第 n 次提交的不可变快照
```

- 读取同样按角色控制：admin / clinician 看到标识化快照；researcher
  看到的是去标识化快照（与 export 同一套规则），不会因为没有标识化
  读取权限而拿到 403。
- 失败原因可区分：患者不存在 → **404 `not_found`**；患者存在但版本号
  不存在 → **404 `version_not_found`**（`detail` 含请求的 version）；
  越权 → **403 `forbidden`**。三者错误体中的 `code` 各不相同。

## 访问控制与去标识化

调用方身份通过请求头提供（演示级认证）：`X-Actor`、`X-Role`。

| 操作 | admin | clinician | researcher |
|---|---|---|---|
| 摄取 / 更新 | ✅ | ✅ | ❌ 403 `forbidden` |
| 标识化读取 `GET /patients/{id}` | ✅ | ✅ | ❌ 403 `forbidden` |
| 去标识化导出 `GET /patients/{id}/export` | ✅ | ✅ | ✅ |
| 版本历史 `GET /patients/{id}/history`、`/versions/{n}` | ✅ 标识化 | ✅ 标识化 | ✅ 去标识化 |

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
GET  /patients/{id}/history                      版本历史（按角色返回标识化/去标识化）
GET  /patients/{id}/versions/{n}                 指定版本快照（同上）
```

错误响应统一为 `{"error": {"code", "message", "detail"}}`；并发与历史
相关的 code：`version_conflict`(409)、`not_found`(404)、
`version_not_found`(404)、`forbidden`(403)。

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
  -H 'Idempotency-Key: submit-1' \
  -d '{"patient_id":"p1","identifiers":{"mrn":"M1"},
       "observations":[{"code":{"system":"http://loinc.org","code":"8480-6"},"value":"120"}]}'

curl -H 'X-Actor: bob' -H 'X-Role: researcher' \
  http://127.0.0.1:8000/patients/p1/export    # 去标识化视图
```

### 复现重启恢复与版本历史

```bash
# 1. 起服务（持久化到本地文件）
CLINICAL_DB=./data/clinical.db uv run uvicorn main:app --port 8000 &

# 2. 写入并更新一次（产生 v1、v2 两个历史版本）
curl -X POST 'http://127.0.0.1:8000/patients?source_version=fhir-r4' \
  -H 'X-Actor: alice' -H 'X-Role: clinician' -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: boot-1' \
  -d '{"patient_id":"p1","identifiers":{"mrn":"M1"},
       "observations":[{"code":{"system":"http://loinc.org","code":"8480-6"},"value":"120"}]}'
curl -X PUT 'http://127.0.0.1:8000/patients/p1?source_version=fhir-r4&expected_version=1' \
  -H 'X-Actor: alice' -H 'X-Role: clinician' -H 'Content-Type: application/json' \
  -d '{"patient_id":"p1","identifiers":{"mrn":"M1"},
       "observations":[{"code":{"system":"http://loinc.org","code":"8480-6"},"value":"130"}]}'

# 3. 停掉进程后用同一路径重启，验证：
kill %1
CLINICAL_DB=./data/clinical.db uv run uvicorn main:app --port 8000 &
curl -H 'X-Actor: alice' -H 'X-Role: clinician' \
  http://127.0.0.1:8000/patients/p1/history        # v1、v2 都还在，含写入时间
curl -H 'X-Actor: alice' -H 'X-Role: clinician' \
  http://127.0.0.1:8000/patients/p1/versions/1     # 重启前 v1 的快照原样可取
curl -X POST 'http://127.0.0.1:8000/patients?source_version=fhir-r4' \
  -H 'X-Actor: alice' -H 'X-Role: clinician' -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: boot-1' \
  -d '{"patient_id":"p1","identifiers":{"mrn":"M1"},
       "observations":[{"code":{"system":"http://loinc.org","code":"8480-6"},"value":"120"}]}'
#   -> 返回 {"patient_id":"p1","version":1}，不产生新版本（幂等重放）
curl -X PUT 'http://127.0.0.1:8000/patients/p1?source_version=fhir-r4&expected_version=2' \
  -H 'X-Actor: alice' -H 'X-Role: clinician' -H 'Content-Type: application/json' \
  -d '{"patient_id":"p1","identifiers":{"mrn":"M1"},
       "observations":[{"code":{"system":"http://loinc.org","code":"8480-6"},"value":"140"}]}'
#   -> 版本从 2 继续递增到 3，历史不断档
```

### 复现老数据升级

```bash
# 1. 造一份"老版本服务"留下的库：只有 resources 当前行，没有历史表
python - << 'PY'
import json, sqlite3
conn = sqlite3.connect("./data/legacy.db")
conn.executescript("""
CREATE TABLE resources (patient_id TEXT PRIMARY KEY, version INTEGER NOT NULL,
                        updated_at REAL NOT NULL, body TEXT NOT NULL);
""")
body = json.dumps({"patient_id": "p1", "active": True,
                   "identifiers": {"mrn": "M1"},
                   "observations": [{"code": {"system": "http://loinc.org",
                                              "code": "8480-6", "display": None},
                                     "value": "120", "unit": None,
                                     "effective": None}]}, sort_keys=True)
conn.execute("INSERT INTO resources VALUES ('p1', 2, 1700000002.0, ?)", (body,))
conn.commit(); conn.close()
PY

# 2. 用当前版本直接打开这份老库
CLINICAL_DB=./data/legacy.db uv run uvicorn main:app --port 8000 &

# 3. 验证：当前内容可读；历史不再为空（回填了当前版本 v2，含原写入时间）；
#    v2 可按版本号取回；老服务没记录过的 v1 明确报 version_not_found
curl -H 'X-Actor: alice' -H 'X-Role: clinician' http://127.0.0.1:8000/patients/p1
curl -H 'X-Actor: alice' -H 'X-Role: clinician' http://127.0.0.1:8000/patients/p1/history
curl -H 'X-Actor: alice' -H 'X-Role: clinician' http://127.0.0.1:8000/patients/p1/versions/2
curl -H 'X-Actor: alice' -H 'X-Role: clinician' http://127.0.0.1:8000/patients/p1/versions/1
#   -> 404 {"error": {"code": "version_not_found", ...}}

# 4. 后续写入从 v2 继续递增到 v3，重启后历史为 [2, 3] 不断档
```

对应的自动化复现见 `tests/test_legacy_upgrade.py`（用旧 schema 建库 →
当前版本打开 → 校验历史回填、按版本取回、继续写、再重启）。

测试覆盖：老库升级（历史回填、按版本回看、不可恢复版本的明确报错、
升级后继续写与再重启）、重启后状态/历史/审计/幂等记录的持久性、
并发提交的成败分类（过期版本 vs 不存在的区分、多写者竞争恰好一个胜出、
冲突 detail 含判定版本号、落盘库上并发后重启状态逐版本一致）、
幂等重试（重试不多出版本、审计一记 success 一记 replayed、重启后重试
仍重放、写入与幂等记录原子提交、失败提交不占用 key、并发同 key 去重）、
版本历史顺序与按版本回溯、历史读取的角色控制与去标识化、三类失败原因
（not_found / version_not_found / forbidden）的可区分性、版本不兼容
（三类拒绝原因可区分）、越权读取与去标识化导出、审计完整性。
