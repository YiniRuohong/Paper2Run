# Paper2Run 工程交接文档

更新时间：2026-09-27

固定 worktree：`/Users/andyzhao/Documents/claude code/Ali_Hackthon/.worktrees/phase0-phase1`

分支：`feat/paper2run-phase0-phase1`

本文用于新会话继续开发 Paper2Run。当前工作区存在大量未提交修改和未跟踪源码，不能通过 Git 状态判断单个功能是否由本次会话新增；继续工作时应直接检查当前文件和测试结果。

## 一、继续开发时必须遵守的约束

- 不 commit，不 push。
- 不安装依赖。
- 不访问网络。
- 不部署外部服务。
- 不运行真实模型、GPU 或真实外部 Runner。
- 测试使用 `.venv/bin/python`、`.venv/bin/pytest`。
- 项目测试通常需要设置 `PYTHONPATH=.`。
- Provider Secret 不能进入浏览器、Runner、日志、SSE、Artifact 或导出包。
- Artifact 必须内容寻址、不可变并校验 SHA-256。
- Action/Event 必须 append-only。
- SQLite 测试不能被表述为已经证明 PostgreSQL 锁语义或真实多 Worker 并发安全。
- 不破坏用户工作区，不覆盖与当前任务无关的既有修改。

## 二、当前工程目标

Paper2Run 的主链路是：

```text
Project
  -> Paper
  -> Extraction
  -> Experiment
  -> Spec Revision
  -> Confirmation
  -> Run
  -> Attempt / Execution
  -> Artifact / Metrics / Report
  -> Export
```

当前已经从基础对象链路推进到受控 Execution 控制面，并开始补 Run 证据读取、报告和导出能力。

## 三、已经实现的功能

### 3.1 Project、身份和项目权限基础

已经有：

- Project、User、Membership。
- 项目成员角色和项目级读写权限检查。
- 跨项目资源访问隔离。
- 基础身份相关路由和 actor 解析。
- API 统一错误和 request id 基础结构。

主要文件：

- `services/api/p2r_api/auth.py`
- `services/api/p2r_api/dependencies.py`
- `services/api/p2r_api/routes/projects.py`
- `packages/persistence/p2r_persistence/models.py`

当前安全限制：完整浏览器 Session、Origin 和 CSRF 闭环仍未完成，见后文待实现部分。

### 3.2 Paper、Extraction、Experiment、Revision 和 Confirmation

已经有：

- Paper 记录和项目归属。
- Extraction 记录。
- Experiment 创建和查询。
- Spec Revision 创建、校验和查询。
- Confirmation 创建。
- Idempotency-Key 基础支持。
- 基础 budget 和 issue 校验。

主要文件：

- `services/api/p2r_api/routes/papers.py`
- `services/api/p2r_api/routes/experiments.py`
- `services/api/p2r_api/routes/runs.py`
- `packages/persistence/p2r_persistence/models.py`

### 3.3 Run 创建、详情、事件和取消

已经有：

```text
POST /api/v1/projects/{project_id}/runs
POST /api/v1/projects/{project_id}/experiments/{experiment_id}/runs
GET  /api/v1/projects/{project_id}/runs
GET  /api/v1/runs/{run_id}
GET  /api/v1/runs/{run_id}/events
POST /api/v1/runs/{run_id}/cancel
```

Run 详情接口已经补齐，当前返回内容包括：

- `run_id` / `id`
- `project_id`
- `experiment_id`
- `revision_id` / `spec_revision_id`
- `status`
- `mode`
- `manifest_hash`
- `metrics_hash`
- `report_hash`
- `verdict`
- 安全过滤后的 `error`
- `last_event_seq`
- `created_at`
- `updated_at`

详情查询会先通过 `_get_run()` 做项目权限检查，并查询 Run 最新事件序号。错误只允许安全字段，不原样透传内部错误 JSON。

主要实现位置：

- `services/api/p2r_api/routes/runs.py:90-151`

主要测试：

- `tests/integration/phase02/test_run_detail_http.py`

Run 取消已经支持 cooperative cancellation：

- queued Run 可以被取消。
- running Run 通过父 Run 的 `cancel_requested` 状态向 Runner 传递取消意图。
- 已完成、失败或已取消的 Run 不会被取消请求覆盖。
- 取消和完成竞态由服务端状态决定。

### 3.4 Execution 控制面

已实现 Execution 生命周期基础：

- ExecutionRecord。
- Execution repository。
- Job claim。
- Runner identity。
- fencing token。
- lease heartbeat。
- finish success/failure/cancelled。
- lease 过期 recovery。
- stale runner 拒绝。
- fencing 失败时回滚业务写入。
- 父 Run cooperative cancellation handoff。

HTTP 接口：

```text
GET  /api/v1/runs/{run_id}/executions
POST /api/v1/executions/{execution_id}/heartbeat
POST /api/v1/executions/{execution_id}/finish
```

Runner 相关实现：

- `packages/orchestration/p2r_orchestration/execution_repository.py`
- `packages/orchestration/p2r_orchestration/run_worker.py`
- `services/runner/p2r_runner_service.py`

相关测试：

- `tests/unit/phase02/test_execution_repository.py`
- `tests/unit/phase02/test_controlled_runner.py`
- `tests/integration/phase02/test_executions_http.py`
- `tests/integration/phase02/test_run_worker.py`

### 3.5 Run Manifest

已实现：

```text
GET /api/v1/runs/{run_id}/manifest
```

当前 Manifest 返回：

- `run_id`
- `status: provisional`
- `manifest_hash`
- `metrics_hash`
- `report_hash`
- Attempts
- Executions
- Exports

安全处理已经包含：

- 不暴露 `runner_id`。
- 不暴露 `fencing_token`。
- 不暴露 `lease_until`。
- 不暴露 storage key。
- Execution error 只投影 `code` 和 `message`。
- 非法 hash 返回 `None`，不直接暴露。
- Export Artifact 只有在 succeeded、Artifact ID 合法且 SHA-256 登记一致时才暴露。
- 不读取 Artifact 内容来伪造 finalized 状态。
- Attempts、Executions、Exports 按确定性规则排序。

主要实现位置：

- `services/api/p2r_api/routes/runs.py:260-342`

主要测试：

- `tests/integration/phase02/test_manifest_http.py`

当前 Manifest 明确是 `provisional`，因为还没有完整 finalized 证据链和 finalized 持久化标记。

### 3.6 Attempts 查询

已实现：

```text
GET /api/v1/runs/{run_id}/attempts
```

目前支持：

- Run 项目权限检查。
- 只返回当前 Run 的 Attempt。
- 按 `attempt_index`、`created_at`、`id` 稳定排序。
- 基础字段白名单。
- 不返回 `validation_json`、`code_manifest_json`、`error_json`。

主要实现位置：

- `services/api/p2r_api/routes/runs.py:210-237`

主要测试：

- `tests/integration/phase02/test_attempts_http.py`

注意：当前 Attempt DTO 仍然是内部模型风格，和公共前端 DTO 尚未完全统一，见待实现部分。

### 3.7 JSON Export 和 Artifact Store

目前已有 `report_json` 导出骨架：

```text
POST /api/v1/runs/{run_id}/exports
GET  /api/v1/runs/{run_id}/exports
GET  /api/v1/artifacts/{artifact_id}/download
```

Artifact Store 已具备：

- 内容寻址。
- 小写 64 位 SHA-256 地址校验。
- 写入时 SHA-256 计算。
- 读取时重新校验 SHA-256。
- 文件篡改检测。
- 文件大小限制。
- 路径逃逸保护。
- 缺失 Artifact 与损坏 Artifact 区分。

主要文件：

- `packages/artifacts/p2r_artifacts/store.py`
- `services/api/p2r_api/routes/runs.py`

主要测试：

- `tests/unit/phase01/test_artifacts.py`
- `tests/integration/phase02/test_export_artifacts_http.py`

当前导出只支持 `report_json`，完整导出状态和 Markdown/PDF/ZIP 尚未完成。

### 3.8 Artifact 文本读取和 Attempt 文件接口的当前雏形

已经新增：

```text
GET /api/v1/attempts/{attempt_id}/files
GET /api/v1/artifacts/{artifact_id}/text
```

当前已有基础逻辑：

- files 从 `AttemptRecord.code_manifest_json["files"]` 读取文件登记。
- files 返回 `path`、`artifact_id`、`bytes`、`sha256`、`media_type`。
- files 基础字段白名单。
- text 只允许指定文本媒体类型。
- text 使用 Artifact Store 做 SHA-256 校验。
- text 校验登记 bytes。
- text 严格 UTF-8 解码。
- `offset` / `next_offset` 按 UTF-8 字节位置处理。
- 最大 `limit` 为 65536。
- 不在多字节字符中间截断。

但是这两个接口还没有闭合，不能算完成。关键问题见下一节。

### 3.9 Curves 查询首版

已经新增：

```text
GET /api/v1/runs/{run_id}/curves
```

当前行为：

- 支持可选 `attempt_id`。
- 指定 Attempt 时检查其属于当前 Run。
- 不指定 Attempt 时选择当前 Run 最近的 Attempt。
- 支持 `max_points`，边界为 1 到 2000。
- 没有曲线持久化数据时返回空 `series`。
- 返回 `downsampled: false` 和 warning。
- 不伪造曲线。
- 不跨 Attempt 拼接训练轨迹。

主要测试：

- `tests/integration/phase02/test_curves_http.py`

注意：曲线接口目前只有安全空结果，没有实际曲线存储和查询。

## 四、当前验证结果

此前完成过以下验证：

```bash
cd /Users/andyzhao/Documents/claude\ code/Ali_Hackthon/.worktrees/phase0-phase1
PYTHONPATH=. .venv/bin/pytest -q
```

结果：

```text
262 passed, 1 warning
```

警告是既有的 Starlette/httpx deprecation warning。

还通过过：

```bash
PYTHONPATH=. .venv/bin/python -m compileall -q services/api packages/persistence
git diff --check
```

最近新增的曲线测试报告为：

```text
8 passed
```

需要注意：后续如果继续修改 Evidence Read Path、导出或模型，必须重新运行聚焦测试和全量测试，不能直接沿用上述旧结果作为最终结果。

## 五、当前最重要的未完成问题

### 5.1 Attempt 文件到 Artifact 文本读取没有闭环

当前 files endpoint 从 Attempt manifest 返回 Artifact ID，但 text endpoint 主要通过 `ExportRecord.artifact_id` 查 Artifact。

因此：

```text
GET /runs/{run_id}/attempts
  -> GET /attempts/{attempt_id}/files
  -> GET /artifacts/{artifact_id}/text
```

这条代码/patch 浏览链路目前可能在第二步之后返回 404。

应建立统一的安全 Artifact 关联查询，至少支持：

- ExportRecord Artifact。
- Attempt manifest 中登记的代码 Artifact。
- Attempt manifest 中登记的 patch、log、evidence Artifact。
- Artifact 必须关联到当前可读的 Run/Attempt。
- Artifact ID、登记 SHA、登记 bytes、实际 store 内容必须一致。
- 不属于当前项目的 Artifact 必须安全返回资源不存在。

推荐优先复用现有 `code_manifest_json["files"]`，暂时不要为了这一步引入过大的新模型；如果后续需要支持 log/evidence，再考虑统一 Artifact registry 表。

### 5.2 files endpoint 目前没有充分验证实际 Artifact

现有实现会检查：

- artifact_id 是合法 hash。
- sha256 和 artifact_id 一致。
- bytes 是非负整数。

但还需要确认：

- Artifact 在 Store 中真实存在。
- Store 内容 SHA 等于 artifact_id。
- Store 内容长度等于登记 bytes。
- Artifact 确实属于当前 Attempt/Run。
- 无法校验的条目应该过滤或返回明确的资源未就绪错误，不应把它作为可读文件返回。

### 5.3 参数错误 envelope 不统一

需要统一检查：

```text
GET /attempts/{attempt_id}/files?...
GET /artifacts/{artifact_id}/text?...
GET /runs/{run_id}/curves?...
```

未知参数应返回项目标准 `VALIDATION_ERROR`。

另一个已知问题是，若 `curves.max_points=not-an-int` 由 FastAPI 直接转换，可能得到框架默认的 `detail` 结构，而不是项目标准错误 envelope。建议改为字符串参数后自行解析，或统一处理 FastAPI 的 RequestValidationError。

### 5.4 UTF-8 offset 行为需要明确

当前实现对非 UTF-8 边界 offset 直接返回 422。

公共契约需要明确采用哪一种：

- 严格模式：offset 必须是合法 UTF-8 边界，否则 `VALIDATION_ERROR`。
- 对齐模式：服务端将 offset 向后对齐到下一个 UTF-8 边界，并返回实际使用的 offset/next_offset。

无论选择哪一种，都要补中文和多字节续读测试，并保证 `next_offset` 始终是字节偏移。

### 5.5 Attempt DTO 尚未与公共前端类型统一

当前后端 Attempt 主要返回：

```text
attempt_id
run_id
attempt_index
repair_ordinal
source
status
base_code_hash
patch_hash
created_at
updated_at
```

前端公共类型期待：

```text
id
run_id
attempt_index
repair_ordinal
status
code_hash
code_manifest_id
patch_artifact_id
parent_attempt_id
error_summary
diagnosis_summary
evidence_artifact_ids
log_artifact_ids
created_at
```

数据库当前也没有完整的：

- code_manifest_id
- patch_artifact_id
- parent_attempt_id
- evidence artifact 列表
- log artifact 列表

需要先决定兼容方案：

1. 在当前响应中增加契约字段，缺失证据使用 `null` 或空数组；
2. 增加新的关联模型和迁移；
3. 暂时保留内部字段并更新前端契约。

新会话应先决定这件事，再继续扩大前端联调。

### 5.6 Attempts/files 分页契约还未完全统一

公共文档要求：

```text
GET /attempts/{id}/files -> Page<CodeFile>
GET /runs/{id}/attempts -> Page<Attempt>
```

当前实现中：

- `/runs/{run_id}/attempts` 返回普通数组。
- `/attempts/{attempt_id}/files` 有分页信息，但 cursor 是简单整数偏移量。

还需要统一：

- Page envelope 形状。
- cursor 是否 opaque。
- limit 的边界。
- 稳定排序。
- 前端 client 的解析方式。

### 5.7 Curves 没有实际数据模型

当前只有：

- `RunRecord.metrics_hash`
- `EvaluationRecord.metrics_json`

缺少过程曲线持久化表或 Artifact 规范。

如果要展示真实曲线，需要增加最小曲线模型，例如：

```text
run_id
attempt_id
series_id
name
source
metric
unit
x_axis
points_json
evidence_artifact_id
downsampled
created_at
```

并添加 Alembic migration、写入逻辑、查询逻辑和每条 series 最多 2000 点的降采样策略。

### 5.8 Report 查询状态不完整

当前 `GET /runs/{run_id}/report` 在没有 Report 时主要返回错误。

公共契约要求支持：

- Run 未终态：pending / waiting_for_run。
- Report 生成中：202 和 operation_id。
- Report ready：200。
- Report 失败：明确错误、operation_id、request_id。

当前还没有完整的 report operation 持久化和查询闭环。

### 5.9 Markdown、PDF、ZIP 导出未完成

当前导出主要支持 `report_json`。

还缺：

- `report_markdown`
- `report_pdf`
- `bundle_zip`
- operation_id
- Operation 查询
- queued/running/succeeded/partial/failed 全状态
- 每个格式独立 item 状态
- partial 语义
- 导出复用语义
- finalized Manifest 和 ready Report 前置条件
- 安全 filename
- 完整导出 Artifact 元数据

公共契约参考：

- `docs/09-前后端公共接口契约.md:454-519`

### 5.10 Runner reconciliation、reaper 和真实取消未完成

当前已经有 heartbeat、finish 和 expired recovery，但还缺：

- Runner 内部 `/internal/v1` 协议。
- handle 对账。
- 独立 reaper。
- recovery 后完整重新排队闭环。
- 重复事件去重。
- 真实进程强制取消。
- 资源回收确认。
- Runner 和 reaper 同时修改状态的完整竞态验证。

### 5.11 派生 Run repair budget 持久化校验不足

当前 Repair Proposal 会更新 Experiment 级 `repairs_used`，但还需要补：

- parent Run 持久化和查询。
- 派生 Run 共享 Experiment repair budget。
- 跨派生 Run 并发预算保护。
- parent/child 审计链。
- 预算耗尽回归测试。

当前 `RunRecord` 是否已真正具备可用的 `parent_run_id` 持久化链，需要新会话再次检查，不要仅依据旧报告假设已经完成。

### 5.12 Session、Origin、CSRF 和综合安全测试未完成

当前已有项目权限检查，但还需要：

- Session 创建、读取、过期、退出。
- secure cookie 行为。
- Origin 校验。
- CSRF token 校验。
- 所有浏览器写请求统一保护。
- 跨项目综合端到端验证。
- 不同角色的读写矩阵测试。

### 5.13 PostgreSQL、多 Worker 和真实 Runner 尚未实测

当前测试基于 SQLite fixture，不能证明：

- PostgreSQL row lock 语义。
- 多 Worker claim 竞争。
- heartbeat/recovery 并发。
- cancellation/finish 并发。
- reaper/runner 竞态。
- 真实进程取消和资源回收。

这些应在允许真实环境后单独安排验收，不应在当前 SQLite 测试通过后直接宣称生产安全。

## 六、推荐的新会话执行顺序

### 第一步：先核对当前工作区和现有实现

新会话开始时先执行：

```bash
cd /Users/andyzhao/Documents/claude\ code/Ali_Hackthon/.worktrees/phase0-phase1
git status --short
PYTHONPATH=. .venv/bin/pytest -q tests/integration/phase02/test_run_detail_http.py tests/integration/phase02/test_manifest_http.py tests/integration/phase02/test_attempts_http.py tests/integration/phase02/test_curves_http.py tests/integration/phase02/test_export_artifacts_http.py
```

然后阅读：

- `services/api/p2r_api/routes/runs.py`
- `packages/persistence/p2r_persistence/models.py`
- `packages/artifacts/p2r_artifacts/store.py`
- `docs/09-前后端公共接口契约.md`
- `apps/web/src/api-client/client.ts`
- `apps/web/src/api-client/types.ts`

### 第二步：优先闭合 Evidence Read Path

建议先完成：

1. Attempt manifest Artifact 安全解析。
2. files endpoint 实际 Store 校验。
3. text endpoint 同时支持 Attempt 文件 Artifact 和 Export Artifact。
4. 中文 UTF-8 offset 续读。
5. bytes/hash 篡改测试。
6. 跨项目资源隔离测试。
7. 未知 query 和非整数参数的统一错误 envelope。

这是当前最适合继续推进的纵向切片，因为它能直接让前端代码预览链路工作起来。

### 第三步：统一公共 DTO

优先处理：

- Attempt 字段命名。
- Page envelope。
- cursor 语义。
- Export DTO。
- Report pending DTO。

统一后再做前后端联调，避免边实现边反复修改字段。

### 第四步：补 Report operation 和导出

顺序建议：

1. Report pending/generating/ready/failed。
2. OperationRecord 或现有 operation 机制复用。
3. report_markdown。
4. bundle_zip。
5. report_pdf。
6. Export item 状态和 partial。
7. 导出复用和幂等性。

PDF 如果当前环境缺少可靠渲染依赖，应先完成接口和可验证的失败语义，不要安装新依赖。

### 第五步：补 Runner reconciliation 和 repair budget

完成：

- reaper。
- handle 对账。
- recovery 重新排队。
- duplicate event 处理。
- parent/child Run。
- 跨派生 Run repair budget。

### 第六步：补浏览器安全和综合验证

最后再补：

- Session。
- Origin。
- CSRF。
- 角色矩阵。
- PostgreSQL 实测条件下的锁和并发验证。
- 真实 Runner/process cancellation 验收。

## 七、关键文件索引

### 后端路由

- `services/api/p2r_api/routes/runs.py`
- `services/api/p2r_api/routes/experiments.py`
- `services/api/p2r_api/routes/projects.py`
- `services/api/p2r_api/routes/auth.py`

### 持久化

- `packages/persistence/p2r_persistence/models.py`
- `packages/persistence/p2r_persistence/job_repository.py`
- `packages/persistence/p2r_persistence/worker.py`
- `packages/persistence/alembic/versions/`

### Orchestration / Runner

- `packages/orchestration/p2r_orchestration/execution_repository.py`
- `packages/orchestration/p2r_orchestration/run_worker.py`
- `packages/orchestration/p2r_orchestration/repair_repository.py`
- `services/runner/p2r_runner_service.py`

### Artifact

- `packages/artifacts/p2r_artifacts/store.py`

### 前端 API

- `apps/web/src/api-client/client.ts`
- `apps/web/src/api-client/types.ts`
- `apps/web/src/api-client/mock/fixtures.ts`
- `apps/web/src/api-client/mock/adapter.ts`

### 主要测试

- `tests/integration/phase02/test_run_detail_http.py`
- `tests/integration/phase02/test_manifest_http.py`
- `tests/integration/phase02/test_attempts_http.py`
- `tests/integration/phase02/test_executions_http.py`
- `tests/integration/phase02/test_curves_http.py`
- `tests/integration/phase02/test_export_artifacts_http.py`
- `tests/integration/phase02/test_run_worker.py`
- `tests/unit/phase02/test_execution_repository.py`
- `tests/unit/phase02/test_controlled_runner.py`
- `tests/unit/phase01/test_artifacts.py`

### 规划和契约

- `docs/superpowers/plans/2026-09-25-execution-control-plane.md`
- `docs/09-前后端公共接口契约.md`
- `docs/06-接口定义.md`

## 八、新会话开场建议

可以直接把下面这段作为新会话第一条消息：

> 继续推进 Paper2Run。请先阅读交接文档 `docs/2026-09-27-paper2run-handoff.md`，固定使用 worktree `/Users/andyzhao/Documents/claude code/Ali_Hackthon/.worktrees/phase0-phase1` 和分支 `feat/paper2run-phase0-phase1`。遵守文档中的禁止 commit、push、安装依赖、联网、真实模型/GPU/外部部署约束。先核对当前文件和聚焦测试结果，再优先闭合 Attempt 文件到 Artifact 文本读取的证据链，补齐完整性校验、UTF-8 字节分块、权限隔离、标准错误 envelope 和回归测试。完成后继续推进 Report operation、Markdown/PDF/ZIP 导出、Runner reconciliation、repair budget、Session/CSRF 和综合验证，直到所有可在当前环境完成的功能都落地，并如实记录 PostgreSQL、多 Worker 和真实 Runner 的外部限制。

## 九、交接结论

当前已完成的是受控 Run/Execution 控制面的主体，包括 fencing、heartbeat、finish、recovery、取消、Run 详情和 provisional Manifest。

当前最需要继续的是面向用户的证据读取链路：Attempt 文件登记、Artifact 关联、文本读取和前端可预览能力。随后才是完整报告状态、Markdown/PDF/ZIP 导出、Runner reconciliation、repair budget、浏览器安全和生产环境并发验证。

现阶段可以说“控制面主体已经建立，证据与交付面仍在补齐”，不能说整个 Paper2Run 已经完成。
