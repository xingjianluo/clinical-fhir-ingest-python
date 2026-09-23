# 临床资源摄取与受控访问服务（本地存储）

面向临床数据互操作的资源摄取（ingest）与受控访问服务。接收多个版本的临床资源
表示并归一到统一内部模型；通过带修订号的乐观并发控制支持同一患者资源的并发更新；
按调用方角色约束读取，并提供去标识化导出；所有判定（允许/拒绝）写入与普通读取
通道隔离的审计记录。后端为纯本地文件存储，无外部依赖。

## 1. 运行与本地验证

要求 Python 3.14+，使用 [uv](https://docs.astral.sh/uv/) 管理：

```bash
# 安装依赖（含 pytest 开发依赖）
uv sync

# 运行全部单测
uv run pytest -q

# 查看测试中的判定日志（输入、版本号、判定依据）
uv run pytest -s --log-cli-level=WARNING

# 启动 HTTP 服务（默认数据目录 ./data，可用 CLINICAL_DATA_DIR 思路下的 create_app 参数替换）
uv run uvicorn main:app --reload
# 健康检查
curl -s http://127.0.0.1:8000/health
```

数据目录布局（全部为本地文件，原子写入）：

```
data/
  patients/<patient_id>.json           当前态
  history/<patient_id>/<rev>.json      不可变历史修订（每次成功写入一份）
  idempotency/<patient_id>/<req>.json  幂等请求快照
  audit/audit.log                      仅追加 JSONL 审计记录
```

## 2. 版本兼容范围

外部资源表示版本由请求体 `version` 字段声明：

| 版本 | 状态 | 说明 |
| --- | --- | --- |
| `1.0` | 支持 | `birthDate`（camelCase）等 v1 字段名会被归一 |
| `1.1` | 支持 | `birth_date`（snake_case），可携带 `conditions`、`active` |
| 其他（含缺失、非字符串） | 拒绝 | `unsupported_version`（422）；类型错误为 `malformed_input`（422） |

必填字段（Patient）：`patient_id`、`name`、`gender`、`birth_date`/`birthDate`。
编码体系白名单（不在白名单内的条件编码一律拒绝）：

- `http://snomed.info/sct`
- `http://hl7.org/fhir/sid/icd-10`
- `http://loinc.org`

**归一化失败绝不静默丢弃**：缺必填字段 → `missing_required_field`；编码体系不可识别
→ `unrecognized_coding_system`；版本不受支持 → `unsupported_version`。错误响应包含
`category`（可区分原因）、`field`、`message` 以及 `raw_hint`（保留原始输入线索）。
被拒绝的输入不会产生任何修订，但拒绝事件会写入审计。

## 3. 乐观并发控制语义

内部修订号 `rev` 从 1 单调递增，与外部 `version`（1.0/1.1）是两个独立概念：

| 场景 | 请求方式 | 结果 |
| --- | --- | --- |
| 新建 | 不带 `X-Expected-Rev`（或为 0） | 成功，`rev=1`；资源已存在则 `version_conflict`（409） |
| 更新 | `X-Expected-Rev: <当前 rev>` | 成功，`rev+1`，旧修订进入不可变历史 |
| 过期更新 | `X-Expected-Rev` 小于当前 rev | `version_conflict`（409），错误信息给出期望与当前 rev |
| 更新不存在的资源 | 任意非零 `X-Expected-Rev` | `not_found`（404）——**与过期冲突明确区分** |

同一患者的写操作在进程内按 patient id 串行化，写盘为“临时文件 + `os.replace`”
原子替换，因此并发提交时同一基准 rev 上恰好一个成功，其余得到明确的 409，
不会丢失更新、不会出现半写文件。

**幂等重试**：写入时携带 `X-Request-Id`，同一 `(patient_id, request_id)` 的重复请求
返回首次结果，不产生重复修订（历史中只保留一次写入）。冲突/校验失败也不会写修订。

## 4. 访问控制与去标识化规则

身份通过请求头传入（本地演示方案）：`X-Actor`（调用方标识）、`X-Role`（角色）。

| 角色 | 完整读取 | 写入/更新 | 去标识化导出 | 读取审计 |
| --- | --- | --- | --- | --- |
| `admin` | 是 | 是 | 是 | 是 |
| `clinician` | 是 | 是 | 否 | 否 |
| `analyst` | 否（仅去标识化视图） | 否 | 是 | 否 |
| 未知/缺失角色 | 否 | 否 | 否 | 否 |

越权访问返回 `forbidden`（403），响应体不含任何患者字段。analyst 的读取视图与
`GET /resources/Patient/{id}/export` 导出文档执行相同的去标识化规则：

- 移除直接标识：`patient_id`、`name`、`request_id`、创建/更新时间戳；
- `birth_date` 归粗为 `birth_year` 与 10 岁年龄段 `age_band`（不保留精确生日）；
- 诊断编码仅保留 `system`/`code`，移除可能含叙述的 `display` 文本。

## 5. 审计

所有 create/update/read/export/read_history/read_audit 尝试（允许与拒绝）均追加到
`data/audit/audit.log`（JSONL），字段含 `audit_id`、时间戳、actor、role、action、
patient_id、rev、resource_version、decision、reason、detail。

- 审计**不经过普通读取接口**：患者读取/导出响应中不含任何审计内容；
- 审计只能通过 `GET /audit/entries`（可按 `?patient_id=` 过滤）访问，且仅 `admin`
  有权限；其他角色访问得到 403，且该越权尝试本身也会被审计。

## 6. HTTP 接口

| 方法 | 路径 | 角色要求 | 说明 |
| --- | --- | --- | --- |
| POST | `/resources/Patient` | admin/clinician | 新建或乐观更新；头 `X-Expected-Rev`、`X-Request-Id` |
| GET | `/resources/Patient/{id}` | 任意已知角色 | 完整视图或去标识化视图（按角色投影） |
| GET | `/resources/Patient/{id}/revisions` | admin/clinician | 历史修订号列表 |
| GET | `/resources/Patient/{id}/export` | admin/analyst | 去标识化导出 |
| GET | `/audit/entries` | admin | 审计查询（支持 patient_id 过滤） |
| GET | `/health` | 公开 | 健康检查 |

错误状态码：422 校验/版本/编码类拒绝；409 乐观锁冲突；404 不存在；403 越权。
所有错误体形如：

```json
{"category": "unrecognized_coding_system", "message": "...", "field": "conditions[0].system", "raw_hint": { ...原始输入... }}
```

## 7. 测试覆盖

- `tests/test_ingest_validation.py`：版本不兼容、必填缺失、编码体系不可识别、
  畸形输入；断言错误归类互斥、字段定位、原始线索保留，判定日志含输入/版本号/依据。
- `tests/test_concurrency.py`：8 线程并发更新恰好一胜、过期 rev 与不存在可区分、
  request_id 重试幂等不重复写入、读-改-写重试循环收敛且无丢失修订。
- `tests/test_access_control.py`：角色矩阵、越权读取不泄露任何敏感字段、
  去标识化导出、审计通道对普通读取隔离且仅 admin 可读。
