# Paper2Run Guided Demo Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付黑白圆角、单主线的 Paper → Extraction → Experiment → Run → Report → Export 本地六步向导，通过真实持久化 API 完成 controlled smoke 和 JSON/Markdown/ZIP 交付。

**Architecture:** 保留旧 Phase01Lab 与 mock 工作台，新建独立 guided surface、严格的产品 DTO 适配层和可测试的流程 controller。显式 demo factory 提供教学文本 fixture 与单消费者任务循环；Report 构建和 Export 打包通过持久化 Job 执行，服务端对象是唯一业务事实来源。前端仅持久化非敏感资源引用、固定请求快照与幂等意图，不使用渲染或动画触发业务写入。

**Tech Stack:** FastAPI、SQLAlchemy、Alembic、SQLite 本地验收、Python 标准库 hashlib/json/zipfile、React 19、TypeScript、Vite、CSS、pytest、Node 原生 test；不增加依赖。

---

## 0. 执行协议与范围

- 规格：`docs/design/specs/2026-09-27-guided-demo-design.md`。对话中已批准；文件内“待审阅”是旧状态，不重新要求设计批准。本计划等待执行批准。
- 所有相对路径均基于 `/Users/andyzhao/Documents/claude code/Ali_Hackthon/.worktrees/phase0-phase1`；分支 `feat/paper2run-phase0-phase1`。
- 不 commit、不 push、不安装依赖、不联网、不部署、不调用模型/GPU/真实训练/外部 Runner。计划中各任务的收尾是 diff 检查，不执行技能默认的 commit 步骤。
- 已有大量未提交修改；每项任务开始前读取所触及文件当前内容，不能 checkout/reset/clean。写任务串行，主 Agent 保留最终 review。
- 首轮不包含完整 PDF 解析、生产 sandbox、真实取消回收、独立 reaper、完整 repair budget、全站 DTO 重写或完整权限矩阵。只做本链路权限回归与本地任务恢复。
- 这是一条耦合的纵向演示链，采用一个计划、三个验收里程碑：A 后端可通过 HTTP 跑通；B 无 UI 的 controller 可恢复；C 六步 UI 和浏览器验收。不要把基础设施缺口藏在 UI 任务中。
- 测试产物放仓库内 `.p2r-guided-verification/`；不主动写 Desktop/Downloads/home/temp。pytest 自己的隔离 fixture 目录沿用既有测试机制。

## 1. 已核对的事实与取舍

| 已存在的代码 | 当前事实 | 本轮处理 |
|---|---|---|
| `services/api/p2r_api/main.py:56` | demo factory 只装配 API、SQLite、Store、KernelSupervisor，无 run job 消费循环 | 仅 demo factory 增加受控单消费者 lifespan |
| `services/api/p2r_api/routes/papers.py:170` | literal 提取、候选为空、引用页码不能保证可靠 | 教学文本专用 adapter；不伪造 PDF 或页码 |
| `services/api/p2r_api/routes/experiments.py:82` | 候选必须已持久化；revision/confirmation 独立写入 | 复用业务 API，逐阶段保存结果 |
| `packages/orchestration/p2r_orchestration/run_worker.py:96` | fixture worker 同事务写 Evaluation 和 Report | Run 只产生 Evaluation，报告移到独立构建任务 |
| `packages/persistence/p2r_persistence/models.py:457` | Report 只有 run_id/content_json/content_hash，无构建状态 | 保持 ready Report 不可变，新增独立 ReportBuildRecord |
| `services/api/p2r_api/routes/papers.py:235` | operation 权限只解析 Paper | 独立按 kind 解析资源权限 |
| `services/api/p2r_api/routes/runs.py:361` | Manifest 是 provisional 动态投影，包含 exports | 报告构建时冻结安全证据快照；不把导出自身递归打包 |
| `services/api/p2r_api/routes/runs.py:735` | 下载只找 ExportRecord 单文件，固定 .json | 支持已登记导出 item，保持 hash/bytes/权限检查 |
| `apps/web/src/main.tsx:7` | `/lab` 直接挂 Phase01Lab；产品 App 属独立 mock surface | `/lab` 改向导，`/lab/advanced` 保留旧工具，`/mock` 不变 |
| `apps/web/src/api-client/types.ts:190` | 理想产品 Run DTO 与真实后端不同 | 新增窄的 guided DTO，不填假 budget/stage/performance |
| `apps/web/tests/*.test.mjs` | Node 原生测试，可直接导入纯 TS | controller/API 采用纯 TS 便于真实行为测试；DOM 另验 |

普通 `create_app()` 不注册 demo 创建端点、不注入 demo 用户、不启动 demo worker。demo-only 与 `development_auth` 分开，不能把任意测试用 supervisor 当作启用 demo 的充分条件。

## 2. 文件责任与新增契约

### 文件地图

| 文件 | 责任 |
|---|---|
| `packages/persistence/p2r_persistence/models.py` | 增加 build/export item/receipt、文本来源元数据 |
| `packages/persistence/alembic/versions/0014_guided_demo.py` | 最小增量迁移及旧 JSON 导出回填 |
| `services/api/p2r_api/demo.py` | 固定 fixture 数据、创建、demo lifespan，不能接收任意代码 |
| `services/api/p2r_api/routes/demo.py` | demo capabilities / bootstrap / receipt 查询 |
| `services/api/p2r_api/routes/operations.py` | operation 权限分派，替代 papers 内单一 Paper 假设 |
| `services/api/p2r_api/routes/reports.py` | 报告构建、状态、显式重试 |
| `packages/orchestration/p2r_orchestration/report_builder.py` | 安全报告与不可变输入快照 |
| `packages/orchestration/p2r_orchestration/export_builder.py` | JSON/Markdown/ZIP 确定性字节生成 |
| `packages/orchestration/p2r_orchestration/report_jobs.py` | report job 分阶段持久化及 fencing |
| `packages/orchestration/p2r_orchestration/export_jobs.py` | export item 逐项状态与复用 |
| `packages/orchestration/p2r_orchestration/run_worker.py` | 保留入口，按 kind 调用 handler；不重复 claim |
| `services/api/p2r_api/routes/runs.py` | 保留 Run/Evidence；接入多格式导出登记和安全下载 |
| `apps/web/src/api-client/guided-types.ts` | 已观测/新增契约，不导入 mock 类型冒充 live |
| `apps/web/src/api-client/guided.ts` | 信封/DTO 校验、稳定 key、取消 GET、字节下载 |
| `apps/web/src/features/guided/state.ts` | 纯 reducer、前置条件、回看与失效规则 |
| `apps/web/src/features/guided/intents.ts` | 非敏感意图日志、序列化请求快照 |
| `apps/web/src/features/guided/controller.ts` | 编排用户动作、恢复、轮询、迟到响应隔离 |
| `apps/web/src/features/guided/useGuidedDemo.ts` | React 订阅/生命周期桥接，不发业务 POST |
| `apps/web/src/features/guided/GuidedDemo.tsx` | shell、导航、焦点与高级入口 |
| `apps/web/src/features/guided/steps.tsx` | 六个小步骤组件，只接数据和回调 |
| `apps/web/src/features/guided/guided.css` | `.guided-demo` 命名空间、响应式、reduced motion |
| `apps/web/src/routing.ts`、`apps/web/src/main.tsx` | 精确 surface 路由与 lazy mount |

### 服务端契约（作为新增接口，不声称现有实现已经支持）

所有 JSON 返回 `{data,meta:{request_id}}`；失败保留已有 ErrorEnvelope。所有 POST，包括 cancel/build/retry，都要求 Idempotency-Key。先权限校验，再返回缓存响应。

```text
GET  /api/v1/demo
  200 {actor_id,fixture:{id,title,source_kind,description},labels}
POST /api/v1/demo/bootstrap
  body {fixture_id:"offline-smoke-v1",source_processing_consent:true}
  201 {project_id,paper_id,extraction_id}
GET  /api/v1/demo/write-receipts/{key}
  200 {state:"completed",target_path,request_sha256,response}
  202 {state:"pending",target_path,request_sha256,response:null}
  404 RESOURCE_NOT_FOUND（不保证此前请求以后不会抵达）
POST /api/v1/runs/{run_id}/report-builds
  body {} 首次构建
  body {retry_of:"build-id"} 只重试当前已失败 build
  202 {build_id,operation_id,run_id,status:"pending"}
GET  /api/v1/runs/{run_id}/report-status
  200 {run_id,status,build_id,operation_id,report_hash,error}
GET  /api/v1/runs/{run_id}/report
  ready: 200 现有 content + 新增 provenance/limitations
  未 ready: 保留 REPORT_NOT_READY 409（兼容旧接口）
GET  /api/v1/operations/{id}
  按 kind 解析 Paper / Run / Export 权限，未知 kind 一律拒绝
POST /api/v1/runs/{run_id}/exports
  body {formats:["report_json","report_markdown","bundle_zip"],include_checkpoint:false}
  202 {export_id,operation_id,status}
GET  /api/v1/runs/{run_id}/exports
  保留 data 数组，每个 export 增加 operation_id/report_hash/items
GET  /api/v1/artifacts/{artifact_id}/download
  从有权限的登记项解析字节；返回对应 MIME/安全文件名/hash/length
```

Report status 的 `pending` 必须伴随已持久化 build。尚未请求构建时返回 `status:null,build_id:null`；前端显示“等待构建”，不要伪造 pending operation。通用 Operation 使用 queued/running/succeeded/failed；只在报告视图映射为 pending/generating/ready/failed。

混合导出允许 `report_pdf` 成为 failed item，错误 `PDF_UNAVAILABLE`，其余项仍处理；未知格式、空数组、重复格式、`include_checkpoint:true` 返回明确 422（本轮不声称 checkpoint 打包）。失败导出显式重试使用新请求 key，仅重建失败项；成功项由内容键复用。未知结果使用原 key、原 body，不能当作失败重试。

### 数据设计

- Paper/Version 新增 `source_kind`（默认 pdf）、`media_type`（默认 application/pdf）；教学文本 page_count=0，不创建虚构 SourceRef.page。fixture quote 以 extraction payload 内文本源引用保存 `source_kind=text_fixture,source_sha256,quote,byte_start,byte_end,page=null`。
- `ReportBuildRecord`：id、run_id、operation_id（唯一）、generation、status、input_json、input_hash、error_json、created_at、updated_at；唯一 `(run_id,generation)`。Run 增加 nullable `active_report_build_id` 字符串引用，避免迁移环形 FK；创建/切换时使用 compare-and-set，ready 后不替换现有 Report。
- `ExportRecord` 增加 report_hash、operation_id；保留旧单文件字段作为兼容投影。
- `ExportItemRecord`：id、export_id、format、status、artifact_id、storage_key、sha256、bytes、media_type、filename、error_json、reuse_key；唯一 `(export_id,format)`。允许多个 item 引用相同 Artifact。
- `ExportArtifactRecord`：reuse_key 主键，run_id、report_hash、format、rules_version、artifact_id/storage_key/sha256/bytes/media_type/filename；仅成功构建写入。reuse_key 包含 Run，不能跨项目借复用绕过权限。
- `DemoWriteReceiptRecord`：actor_id、key、target_path、request_sha256、response_json、project_id（nullable，仅 bootstrap 创建前为空）；唯一 `(actor_id,key)`。只服务 demo 白名单写入，禁止成为任意幂等缓存读取接口；与业务写同事务保存成功 receipt。
- 旧 Report 行迁移后仍然 ready，可从 `ReportRecord` 直接推导；新 status GET 不要求伪造旧 operation。

## Task 1：冻结 HTTP 契约和数据库迁移

**Files:** 修改 `models.py`；新建 `0014_guided_demo.py`、`tests/integration/phase02/test_guided_schema.py`；修改 `docs/engineering/09-前后端公共接口契约.md`（仅添加 Local guided demo 章节）。

- [ ] 1. 写迁移测试：空库 upgrade head；旧库在 0013 插入 Paper/Report/Export 后 upgrade；旧 report/JSON metadata 保留；重复 build generation、重复 export format、重复 receipt 被数据库拒绝。
- [ ] 2. 执行 `PYTHONPATH=. .venv/bin/pytest tests/integration/phase02/test_guided_schema.py -q`，确认新增表/字段缺失导致失败，不接受 import 环境错误作为目标红灯。
- [ ] 3. 按上述数据设计新增模型与迁移，采用既有 UTCDateTime/BIGINT/TimestampMixin。示例约束必须同时出现在 ORM 与迁移：

```python
__table_args__ = (
    UniqueConstraint("run_id", "generation", name="uq_report_build_generation"),
    CheckConstraint("status IN ('pending','generating','ready','failed')", name="ck_report_build_status"),
)
```

  迁移旧 ExportRecord 时仅对完整 metadata 创建 ready item；缺元数据的旧记录保留原状态、不猜造 sha/bytes。downgrade 只删除本次新增表/列；迁移测试使用 Alembic Config，不修改真实 demo 数据库。
- [ ] 4. 增加 API 契约示例与状态映射，明确 `report-status` 与原 `/report` 兼容性、文本 source 不叫 PDF、列表当前无分页则规范化为 `has_more:false,next_cursor:null`。
- [ ] 5. 重跑定向测试与 `git diff --check`，审阅改动文件，不提交。

## Task 2：真实教学 fixture、Demo 开关与未知写结果查询

**Files:** 新建 `services/api/p2r_api/demo.py`、`routes/demo.py`、`tests/integration/phase02/test_guided_bootstrap.py`；修改 `main.py`、`papers.py`、`experiments.py`、`runs.py` 中白名单写响应保存点。

- [ ] 1. 写 bootstrap API 测试：普通 app 404；demo GET 无写副作用；无 consent 422；同 key 同 body 返回相同三个 ID；同 key 改 body 409；错误 actor 不能读取他人 receipt；第二次 GET source 字节 hash 与 Paper 登记一致。

```python
def test_bootstrap_reuses_identity(demo_client):
    body = {"fixture_id": "offline-smoke-v1", "source_processing_consent": True}
    headers = {"X-Actor-ID": "demo-user", "Idempotency-Key": "bootstrap-1"}
    a = demo_client.post("/api/v1/demo/bootstrap", headers=headers, json=body)
    b = demo_client.post("/api/v1/demo/bootstrap", headers=headers, json=body)
    assert a.status_code == b.status_code == 201
    assert a.json()["data"] == b.json()["data"]
```

  `demo_client` 在该测试模块定义为 TestClient 包装显式 demo factory、独立 SQLite/Store、禁用自动 worker，不能把直接数据库注入当成 bootstrap 验收。
- [ ] 2. 运行 `PYTHONPATH=. .venv/bin/pytest tests/integration/phase02/test_guided_bootstrap.py -q`，先红。
- [ ] 3. 固定文本内容及候选仅在服务端定义：

```python
FIXTURE_ID = "offline-smoke-v1"
FIXTURE_TEXT = (
    "Paper2Run teaching fixture.\n"
    "This is a controlled smoke check of the offline fixture recipe.\n"
    "It is not a research paper and not a full reproduction.\n"
).encode("utf-8")
CANDIDATE = {
    "id": "offline-smoke", "recipe_id": "offline_fixture", "recipe_version": "1",
    "label": "Controlled smoke", "execution_support": "supported",
    "provenance": "manually_configured_recipe",
}
```

  bootstrap 单事务创建专用 Project/Membership/Paper/Version/Extraction，Store.put_bytes 先生成实际 hash。仅创建固定 `demo-user`，不允许请求体传任意 actor/项目所有权/候选/代码。失败不得留下半条数据库链；Store 孤立不可变对象不作为成功证据。
- [ ] 4. 修改 Paper 内容读取依据 media_type；Store 读取后比较 sha256 和 bytes。`_paper_json` 查询真实 current extraction/operation，别继续常量 None。普通 PDF 提取不受 fixture 影响；文本 extraction 再请求时复用既有唯一 `(paper_id,content_sha256)`，避免 unique crash。不为未验证 literal strings 生成假页码。
- [ ] 5. 在 demo 白名单写路径同事务登记 receipt（bootstrap、experiment、revision、confirmation、run、cancel、report build、export）。请求 identity 用原始 UTF-8 body sha256，响应只保存已允许公开 DTO。查询先按 actor 查 key，再验证项目权限；被撤销权限返回 404。未知 key 查询 404 后只允许显式原 key 重放，不能推断“绝对未创建”。
- [ ] 6. 上述测试加 malformed consent/未知 fixture/source hash 损坏/撤销 membership/同 key 不同 target 测试，重跑通过。

## Task 3：独立报告构建与操作权限

**Files:** 新建 `report_builder.py`、`report_jobs.py`、`routes/reports.py`、`routes/operations.py`、`tests/integration/phase02/test_report_builds_http.py`、`tests/unit/phase02/test_report_builder.py`；修改 `run_worker.py:96-109`、`papers.py:235`、`routes/__init__.py`、既有 run/export 测试的报告准备步骤。

- [ ] 1. 写状态测试：completed Run 无报告；GET status 连续两次不创建 Job；POST 建 pending；claim 后 generating 可在另一 session 读到；处理后 ready；构建异常 failed；显式 retry 新 generation；旧 retry_of 409；ready 重复构建复用且不改内容。
- [ ] 2. 运行 `PYTHONPATH=. .venv/bin/pytest tests/integration/phase02/test_report_builds_http.py tests/unit/phase02/test_report_builder.py -q`，先红。
- [ ] 3. 将 worker 内 report 创建移除，保留 Evaluation、Run terminal、Attempt/Execution 状态与 fencing。既有测试改为“Run 完成不等于报告 ready”，再显式 POST/处理 report job；不要保留第二条偷偷同步创建 Report 的路径。
- [ ] 4. 定义 `build_report(snapshot: dict) -> dict` 纯函数，只读取快照白名单：

```python
LIMITATIONS = ["local demo", "controlled smoke", "not full reproduction"]

def build_report(snapshot: dict) -> dict:
    return {
        "schema_version": "guided-report-v1",
        "run_id": snapshot["run_id"],
        "revision_id": snapshot["revision_id"],
        "recipe": snapshot["recipe"],
        "metrics": snapshot["metrics"],
        "metrics_source": "controlled_fixture_evaluation",
        "evaluator_version": snapshot["evaluator_version"],
        "verdict": "not_comparable",
        "manifest": snapshot["manifest"],
        "evidence": snapshot["evidence"],
        "limitations": LIMITATIONS,
    }
```

  入快照前校验 revision/spec、Evaluation metrics_hash、原文 hash；从允许键构造 metrics，不能 deepcopy 整个任意 payload。manifest 明确保留 provisional，不包含后续 exports；时间采用已持久化时间，不用打包时刻改变内容 hash。证据记录以当前 Run 的实际登记关系核对。
- [ ] 5. 报告 POST：先 `write_check` 和幂等；要求 Run completed；用 active build ID compare-and-set 原子占位；新 generation + Operation + Job 同事务。retry 只允许当前 failed build，新 ID 保留旧失败审计；ready 返回已有报告，不允许覆盖。receipt 与响应一起提交。
- [ ] 6. report job：先 commit claim + generating，纯字节/报告构建在事务外；最终事务以 job owner/token 与 active build ID 双重检查，成功写 Report/Run.report_hash/operation；失效 token 不得写成功/失败状态。异常存固定 `REPORT_BUILD_FAILED` 安全文案，不把 str(exc) 入公共 DTO/Job.error。中断后沿用同一 payload_ref 恢复，重复完成不重复 Report。
- [ ] 7. operation endpoint 移出 papers：kind paper_ingestion/paper_extraction → Paper；report_build → Run；export_bundle → Export → Run；未知 kind、越权返回 404，不能 fallback 任意类型。保留旧 paper 测试通过。
- [ ] 8. 针对 fence 失败、输入 hash 损坏、跨项目、字符串 secret canary 做测试；重跑新测试及 `test_run_worker.py`。fencing 回滚期望要区分“claim 已提交”与“业务完成未提交”，不能沿用旧的全部 queued 断言误判。

## Task 4：JSON / Markdown / ZIP 内容构建与逐项导出

**Files:** 新建 `export_builder.py`、`export_jobs.py`、`tests/unit/phase02/test_export_builder.py`、`tests/integration/phase02/test_guided_exports.py`；修改 `runs.py:505-533,622-773`、`run_worker.py:154-195`、`test_export_artifacts_http.py`。

- [ ] 1. 写 bytes 测试：JSON 解析可还原报告；Markdown 含三条限制及 metrics_source；相同快照两次 ZIP bytes 一致；坏 hash/bytes、缺必要证据、绝对路径、`..`、反斜线、重复 archive name 均拒绝。
- [ ] 2. 运行 `PYTHONPATH=. .venv/bin/pytest tests/unit/phase02/test_export_builder.py tests/integration/phase02/test_guided_exports.py -q`，先红。
- [ ] 3. 在 `export_builder.py` 定义 `canonical_json(value) -> bytes`、`render_markdown(report) -> bytes`、`build_zip(entries: list[tuple[str,bytes]]) -> bytes`。Markdown 对标题/表格换行和竖线转义；不生成 raw HTML。ZIP 使用固定日期和排序：

```python
import io
import zipfile
from pathlib import PurePosixPath

def build_zip(entries: list[tuple[str, bytes]]) -> bytes:
    out = io.BytesIO()
    names = set()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, data in sorted(entries):
            path = PurePosixPath(name)
            if not name or path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name or name in names:
                raise ValueError("UNSAFE_ARCHIVE_PATH")
            names.add(name)
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
    return out.getvalue()
```

  只打包 `report.json`、`report.md`、`manifest.json`、`metrics.json`、`checksums.json` 和经登记校验的 `evidence/<sha>.txt`。报告构建快照冻结的 manifest/metrics 原样使用；不扫描 workspace、不包含环境变量/provider配置/源数据库。checksums 列出其余文件（不递归列自己）。
- [ ] 4. 增加 `ExportArtifactRecord` 成功复用键：canonical hash of `{run_id,report_hash,format,rules_version:"guided-export-v1"}`。再次使用仍读 Store 校验，不信任旧 ready 标志。每个 ExportItem 单独 pending/running/ready/failed，整体按 ready 数量为 succeeded/partial/failed。PDF item 固定失败但 JSON/Markdown 可成功。
- [ ] 5. Export POST 在入队前固定 report_hash，GET/List 保留旧字段并新增 items；新 handler 支持 `export_bundle`，兼容已有 `export_report_json` 积压 job（进入同一实现），不能直接令旧 job unsupported。单格式 JSON 仍投影 artifact_id 以兼容旧读者。
- [ ] 6. 下载与 text 注册查询支持 ExportItem；按每条登记 Run 检查权限，避免相同内容 hash 第一个记录越权导致拒绝正确授权项。验证元数据 hash/bytes/media_type 再返回，Content-Disposition 从 format 白名单构造 ASCII 文件名，不使用用户文本。
- [ ] 7. 集成测试覆盖三格式、partial、同 key 相同/不同 body、不同 key 成功复用、失败显式新请求、导出进行时重启、损坏复用项、跨 Run 证据、canary secret 不出现在报告/ZIP/错误/Event。执行新测试加既有 `test_export_artifacts_http.py`。

## Task 5：Demo 单消费者、运行可观测状态与重启

**Files:** 修改 `demo.py`、`main.py`、`run_worker.py`、`job_repository.py`；新建 `tests/integration/phase02/test_guided_runtime.py`；更新 `test_demo_runtime.py`。

- [ ] 1. 写 lifespan 测试：无需直接调用 process_next_run_job，从 bootstrap/实验/Run HTTP 到 completed，再 report/export HTTP 到 ready；普通 create_app 不消费；关闭无遗留线程；重启 queued/report generating/export running 后恢复。
- [ ] 2. 执行 `PYTHONPATH=. .venv/bin/pytest tests/integration/phase02/test_guided_runtime.py -q`，先红。
- [ ] 3. `JobRepository.claim` 增加可选 kinds 过滤（默认 None 保留现有语义），让 demo worker 只消费 `run_fixture,report_build,export_bundle,export_report_json`，不能抢 Session job 后标失败。使用一个 stop event 和一个后台线程；线程不执行请求提供的代码。

```python
while not stop.is_set():
    worked = process_next_run_job(session_factory, artifact_store, worker_id="guided-demo")
    if not worked:
        stop.wait(0.2)
```

  `process_next_run_job` 内 claim 指定其支持的 kinds；同一 job 只 claim 一次再分派 handler。lifespan 退出 signal + 有界 join，不能创建无穷 daemon 漏洞。
- [ ] 4. Run 的 queued/running/evaluating/terminal 必须在独立读取中可见，分离 claim/start 与最终落库事务；不通过 sleep 伪造阶段。取消读取刷新新 session，显示 cancel_requested 直到 worker terminal。实际时间来自 Execution started/finished；前端不合成训练进度。
- [ ] 5. 启动只回收已过期且属于受控 job kinds 的 lease；无 handle 的纯 report/export 可安全重放，Run 仅固定内存 fixture 可重算且 Evaluation/Attempt 去重。未过期任务显示等待 lease，不强夺活跃 ownership。此逻辑不对外声称解决真实 Runner reconciliation。
- [ ] 6. 修正 fixture worker 公共错误输出：固定 PIPELINE_FAILED 消息，不持久化 str(exc) 中可能有的凭据。注入 canary 验证所有新路径。沿用 lost-fence、重复 terminal、取消测试，按分阶段事务语义更新断言，禁止删测试规避问题。
- [ ] 7. 运行 Task 1–5 全部新增测试和 `tests/integration/phase01 tests/integration/phase02 tests/unit/phase01 tests/unit/phase02`。到此完成里程碑 A，记录真实结果。

## Task 6：Guided API 客户端与真实 DTO

**Files:** 新建 `guided-types.ts`、`guided.ts`、`apps/web/tests/guided-api.test.mjs`。不改 mock fixtures，不把旧 Session API 替代产品 API。

- [ ] 1. Node 测试先覆盖请求 key、body bytes、actor、未知响应、202/409、404、坏 envelope、非整数 cursor、下载 MIME/length/hash，使用注入 fetcher，不需要网络。
- [ ] 2. `pnpm --dir apps/web test` 先红，确认失败来自新方法/校验缺失。
- [ ] 3. 新增窄 DTO；RunStatus 保留后端 running/evaluating，不强 cast 为 mock 的 executing/verifying：

```ts
export type Step = 'paper' | 'extraction' | 'experiment' | 'run' | 'report' | 'export';
export type ReportState = 'pending' | 'generating' | 'ready' | 'failed';
export interface WriteIntent {
  key: string; path: string; body: string;
  state: 'prepared' | 'unknown' | 'accepted';
}
export interface GuidedRun {
  run_id: string; project_id: string; experiment_id: string; revision_id: string;
  status: 'queued' | 'running' | 'evaluating' | 'cancel_requested' | 'completed' | 'failed' | 'cancelled' | 'awaiting_revision';
  last_event_seq: number; created_at: string; updated_at: string;
  error: {code: string; message: string} | null;
}
```

  其余 DTO 按第二节 response 定义，Experiment 使用 experiment_id，Attempt 使用 attempt_id。缺必要 ID/关联/状态立即 INVALID_DTO；不补不存在的 performance/budget。page 适配只接受真实数组及可选 page，next_cursor 原样编码传递；不得在浏览器解析 cursor。
- [ ] 4. `createGuidedApi(actorId,fetcher=fetch)` 提供 read(path,signal)、write(intent)、receipt(key)、download(item)。write 直接发送保存的 body 字符串和 key，不内部 uuid、不自动 retry；网络/5xx/成功但坏 JSON 保留 unknown。GET 取消 AbortError 不显示为业务失败。
- [ ] 5. 方法路径常量对应第二节。所有 id encodeURIComponent。Run events 按真实 seq/type/payload 解析有限 SSE（不要复用 Session.sequence_no 当 Run.seq）；去重交给 controller。Artifact text offset/next_offset 始终整数 byte，不用 JS text.length 计算下一页。
- [ ] 6. 文件下载通过 fetch（开发 actor header 不能靠 window.location 附带），Blob 前校验 SHA-256 与 bytes，再触发 object URL 并及时 revoke；失败不给用户看作成功下载。不把 server storage_key 发进浏览器。
- [ ] 7. 重跑 `pnpm --dir apps/web test && pnpm --dir apps/web typecheck`。

## Task 7：可恢复的流程状态与意图日志

**Files:** 新建 `state.ts`、`intents.ts`、`controller.ts`、`apps/web/tests/guided-state.test.mjs`、`apps/web/tests/guided-controller.test.mjs`。

- [ ] 1. 写 reducer 单测：未审阅不可进入 Experiment；未 confirmation 不可启动；failed/cancelled 不进入报告；ready report 不自动跳 Export；返回查看不写；编辑上游使下游 active refs 失效但历史 refs 保留；旧 epoch 响应被忽略。
- [ ] 2. 写 controller 假 transport 行为测试：Experiment 创建成功但 revision 失败，继续只重试 revision；确认成功后刷新失败只 GET；StrictMode 两次订阅不重复 POST；从保存 refs 恢复不取列表第一条；身份变化中断旧请求。
- [ ] 3. 运行 `pnpm --dir apps/web test`，先红。
- [ ] 4. state 区分 viewStep、已验证 refs、reviewedExtractionId、pending intents、epoch；成功由响应 DTO/恢复查询推进，动画无权 dispatch 业务完成。事件以 `${run_id}:${seq}` Map 合并且排序，切 Run 清空对应活动视图。
- [ ] 5. `intents.ts` 使用注入 Storage；namespace `p2r.guided.v1:<actor>`。写前 persist key/path/body，禁止 arbitrary object dump。只保存固定公开字段；不保存 auth token/CSRF/secret/全文日志。Storage 写失败先报错，不发送不可恢复的 POST。

```ts
export function prepareIntent(path: string, body: unknown): WriteIntent {
  return { key: crypto.randomUUID(), path, body: JSON.stringify(body), state: 'prepared' };
}
```

  `WriteIntent` 从 `guided-types.ts` type-only 导入。restore 时验证 path 属白名单、body schema/大小、版本、actor、资源关系；本地记录不算授权证明。
- [ ] 6. controller 暴露 startExample、reviewExtraction、confirmExperiment、startRun、retryReport、createExports、refresh、viewStep、editExperiment、dispose。Experiment 创建发生在 reviewExtraction 用户操作之后；confirmExperiment 按 draft → revision → confirmation 串行，每步立即存 ID。revision body 来自服务器 draft，只发送后端允许字段；首轮 editable_fields 为空时只显示只读配置和 budget 同意 checkbox，不伪造可编辑参数。
- [ ] 7. startRun 保存“本次启动包括完成后构建报告”的意图；活跃 command 的 Run 轮询发现 completed，先查 report-status，再准备并只发送一次 report build。刷新恢复只 GET：若尚未建报告则显示显式“生成报告”，不得 mount/effect 补发 POST。这是自动推进与刷新不写的明确边界。
- [ ] 8. unknown 先 query receipt，校验 target_path/request_sha256，再恢复 response。pending 只查询；404 显示“结果仍未确认，可使用同一请求重试”，用户显式重放原 key/body。accepted 后读失败保留 accepted，refresh 不可 write。403/404 当前 refs 失效并清除敏感视图，无 mock fallback。
- [ ] 9. 编辑是显式动作：只回看不失效；请求新 revision 前保留历史 Run/report/export refs，base_revision_id 使用服务器 head，冲突展示 REVISION_CONFLICT 不强写。缺候选/unsupported/blocking 均停留解释。
- [ ] 10. 全部 controller 测试通过并 typecheck，完成里程碑 B。

## Task 8：六步单主线界面与入口切换

**Files:** 新建 `useGuidedDemo.ts`、`GuidedDemo.tsx`、`steps.tsx`、`guided.css`、`apps/web/tests/guided-ui.test.mjs`；修改 `main.tsx`、`routing.ts`、既有 `integration.test.mjs` 精确入口断言。

- [ ] 1. 写路由测试：`/lab`、`/lab/` → guided；`/lab/advanced`、尾斜线 → lab；`/laboratory`、`/lab/unknown` 不匹配；所有 VITE_DATA_MODE 下 guided 不 fallback mock。保留旧 `/mock` 测试。
- [ ] 2. `pnpm --dir apps/web test` 先红。
- [ ] 3. useGuidedDemo 仅订阅 controller/getSnapshot，effect 仅 GET 恢复/清理；用户按钮回调才启动命令。shell 一次只挂当前 step，已完成步骤允许回看，未满足步骤 disabled。六个组件共享 `data / busy / error / onPrimary / onBack` 的明确 props，不自行 fetch。
- [ ] 4. Paper 显示教学 fixture 来源、说明和 consent；Extraction 显示真 quote/hash、人工 recipe 标签及缺失字段；Experiment 显示只读 smoke/预算范围和显式同意，不宣称实际消费；Run 显示真实状态/耗时/最近事件、次要取消；Report 显示构建四态、来源/限制/显式重试；Export 显示格式选择、逐项状态、校验后下载，PDF disabled 并解释。
- [ ] 5. CSS 只在 `.guided-demo` 下生效：

```css
.guided-demo { min-height: 100vh; background: #fafafa; color: #171717; }
.guided-demo .wizard-main { width: min(100% - 32px, 760px); margin: 48px auto; }
.guided-demo .step-card { border: 1px solid #e5e5e5; border-radius: 12px; background: #fff; padding: 28px; animation: guided-enter 240ms ease-out; }
.guided-demo button:focus-visible, .guided-demo a:focus-visible { outline: 2px solid #171717; outline-offset: 3px; }
@keyframes guided-enter { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }
@media (prefers-reduced-motion: reduce) { .guided-demo .step-card { animation: none; } }
@media (max-width: 600px) { .guided-demo .wizard-main { margin: 24px auto; } .guided-demo .step-card { padding: 20px; } }
```

  不加彩色渐变/大仪表盘/模拟百分比。顶部进度用紧凑文字，在窄屏可换行；每步一个主按钮。详情用原生 details/summary 放 Events/Attempts/Executions/hash，旧工具链接 `/lab/advanced`。
- [ ] 6. 步骤切换将焦点放新标题（tabIndex=-1），错误聚焦 role=alert，加载用 aria-busy、状态用 aria-live=polite。返回编辑与取消不藏在动画中，取消文案明确“请求取消不代表已经终止”。
- [ ] 7. 新 surface lazy load，不加载 MockWorkspace 的全局 CSS/auth/getClient；只保留一个 createRoot。entry unavailable 文案更新为“受控纵向 demo 可用，完整生产产品未接入”，不继续说报告完全没接入。
- [ ] 8. 运行 `pnpm --dir apps/web test && pnpm --dir apps/web typecheck && pnpm --dir apps/web build`。Node 源码断言只证明结构，不声称完成 DOM/视觉验收。

## Task 9：端到端、故障注入和证据交付

**Files:** 新建 `tests/integration/phase02/test_guided_journey.py`、`docs/verification/2026-09-27-guided-demo.md`；修改 `README.md` 中 demo 入口/边界；不改无关部署章节。

- [ ] 1. 从空 demo 数据目录经 HTTP 完成 bootstrap → extraction review → experiment → revision → confirmation → run → report-build → export。测试不能直接插 DB 作为成功主线前置，不手动填内部 ID，不直接调用 worker。所有 hash/bytes 从下载重新计算。
- [ ] 2. 故障测试分别覆盖：Run failed/cancelled、缺 candidate、权限变化、revision conflict、重复 events、断网未知结果、POST 成功后 GET 失败、服务重启、迟到响应、bad hash/缺文件/ZIP 路径穿越、同 key 冲突、partial/reuse。使用已安装测试机制，不安装浏览器依赖。
- [ ] 3. 全量命令（从 worktree 根执行）：

```bash
PYTHONPATH=. .venv/bin/pytest tests/unit tests/integration -q
PYTHONPATH=. .venv/bin/python -m compileall -q packages services
pnpm --dir apps/web test
pnpm --dir apps/web typecheck
pnpm --dir apps/web build
git diff --check
```

  预期全通过；记录实际数量和 warning，不复制旧的 264/22 历史结果。
- [ ] 4. 启动仅 loopback 的验证服务（后台进程记录任务 ID，用 task_stop 收尾）：

```bash
P2R_DEMO_DIR=.p2r-guided-verification/runtime PYTHONPATH=. .venv/bin/python -m uvicorn p2r_api.main:create_demo_app --factory --host 127.0.0.1 --port 8010
P2R_API_TARGET=http://127.0.0.1:8010 pnpm --dir apps/web dev --host 127.0.0.1 --port 5173
```

  已占用端口先识别，不终止别人的进程。旧 schema 目录不静默丢弃：迁移或明确提示需升级；验收使用新目录。
- [ ] 5. 浏览器检查 `http://127.0.0.1:5173/lab` 桌面和 390px 窄屏、键盘遍历、错误焦点、reduced motion、每步刷新、返回编辑、旧高级工具和 mock 隔离。选择当前可用浏览器工具前加载对应 skill；不访问外网。若环境不可操作浏览器，标记“视觉验收未执行”，不能以 build 通过代替。
- [ ] 6. 验证记录包含启动命令、实测状态、导出 hash/bytes、失败场景、截图（若实际创建）、已知限制及未验证项目。明确 SQLite 未验证 PostgreSQL 锁/多 Worker、非生产身份、无 hostile-code sandbox、provisional manifest、固定指标不是论文性能。
- [ ] 7. 主 Agent review diff 与证据，更新任务清单；交付实际创建/修改的相关文件，按 surface deliver-assets 输出，不把仅读文件作为本轮成果。保持不 commit/push。

## 3. 自审映射

| 规格条款 | 任务 |
|---|---|
| §1–2 边界、真实后端断点 | 1–5 |
| §3 布局/动效/键盘/reduced motion | 8–9 |
| §4 Paper/Extraction 和人工 recipe 区分 | 2、7、8 |
| §4 Experiment 多阶段幂等、预算确认 | 2、6、7 |
| §4 Run 明确启动、真实事件/取消 | 5、7、8 |
| §4 Report 四态、GET 无副作用、显式重试 | 3、7 |
| §4 Export 多格式、复用、partial、校验 | 4、6 |
| §5 DTO/opaque cursor/UTF-8 | 1、6 |
| §6 刷新、未知写、迟到响应、历史保留 | 2、6、7 |
| §7 权限/秘密/真实性 | 2–6、9 |
| §8 验收 | 各任务红绿测试 + 9 |

执行依赖顺序：1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9。新文件名/接口名保持本文一致；执行中若必须改变数据模型或公共行为，先回报差异而不是自行扩展范围。

## 4. 计划阶段验证声明

本文件是实施计划，不是已完成实现。编写时只读核对现有源码、测试与模型；尚未运行本计划中的新测试、迁移、浏览器验收或实现新接口。待用户批准执行后按任务交付；本轮不以历史测试通过数作为新计划正确性证明。
