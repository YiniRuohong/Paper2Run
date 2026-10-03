# Paper2Run 文章到实验执行闭环实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将现有固定 controlled-smoke 向导扩展为文章输入、Provider 结构化分析、环境研判、用户确认后本机执行以及手动 SSH 分流的真实可验证闭环，同时保持 secret、幂等和报告真实性边界。

**Architecture:** 复用现有 Paper/Extraction/Experiment/Run/Report/Export 资源和 `GuidedController`，新增受控的 article-input、provider-session、environment-check 与 SSH verification 边界。Provider 只返回版本化结构化结果，实验执行只接受注册 Recipe；本机和 SSH 共享 Run/Report 状态，Blocked/Unsupported 不创建虚假 Run。

**Tech Stack:** FastAPI、Pydantic v2、SQLAlchemy/Alembic、React/TypeScript/Vite、现有 Node 测试、pytest/httpx、fake Provider transport、fake environment probe、fake SSH executor。

---

## 实施约束与执行顺序

- 工作目录固定为 `/Users/andyzhao/Documents/claude code/Ali_Hackthon/.worktrees/phase0-phase1`。
- 不执行 `git reset`、`git checkout`、`git clean`，不覆盖现有 modified/untracked 文件。
- 不安装依赖、不联网部署、不调用真实模型/GPU/外部 Runner；Provider、环境检查和 SSH 使用可替换 fake 实现验证。
- 不提交或推送 commit；每个任务以定向测试、全量测试和 `git diff --check` 作为检查点。
- 先完成后端契约和安全边界，再接前端，最后做浏览器交互验收；不要在旧固定 fixture 和新真实入口之间创建第二套持久化事实来源。

## 文件边界总览

### 新建

- `packages/persistence/alembic/versions/0015_article_provider_environment.py`：新增 Provider session、article input metadata、environment check、SSH verification 的迁移。
- `packages/orchestration/p2r_orchestration/article_schema.py`：结构化文章抽取、ExperimentSpec、source_refs 和支持级别的 Pydantic schema。
- `packages/orchestration/p2r_orchestration/provider_sessions.py`：运行时 secret store、Provider session 生命周期和安全投影。
- `packages/orchestration/p2r_orchestration/environment_checker.py`：本机检查器协议、检查项和 Ready/Blocked/Unsupported 决策。
- `packages/orchestration/p2r_orchestration/ssh_executor.py`：受控 SSH verifier/executor 协议；默认 fake/injected 实现，不接收密码或任意命令。
- `services/api/p2r_api/routes/provider_sessions.py`：Provider 配置、测试、清除接口。
- `services/api/p2r_api/routes/environment.py`：环境检查、SSH 验证和执行目标接口。
- `tests/unit/phase02/test_article_schema.py`：抽取 schema 和 Recipe 支持级别测试。
- `tests/unit/phase02/test_provider_sessions.py`：secret 不落盘、生命周期和安全投影测试。
- `tests/unit/phase02/test_environment_checker.py`：环境项与决策测试。
- `tests/unit/phase02/test_ssh_executor.py`：SSH 输入边界和验证状态测试。
- `tests/integration/phase02/test_article_entry.py`：文章输入、Provider 抽取和幂等 API 测试。
- `tests/integration/phase02/test_environment_routes.py`：环境/SSH/API 权限和 Run 创建边界测试。
- `apps/web/src/api-client/article-types.ts`：文章、Provider、Environment、SSH DTO。
- `apps/web/src/api-client/article.ts`：新入口 API 适配与错误映射。
- `apps/web/tests/article-entry.test.mjs`：输入和 Provider 表单行为测试。
- `apps/web/tests/environment-flow.test.mjs`：Ready/Blocked/Unsupported/SSH 分流测试。

### 修改

- `packages/persistence/p2r_persistence/models.py`：加入新资源模型和关系，沿用既有 ID/时间/status 约定。
- `packages/persistence/p2r_persistence/worker.py`：注册受控 extraction/environment/SSH job，保持 worker 幂等和 fencing。
- `packages/orchestration/p2r_orchestration/providers.py`：补充结构化 schema 请求、调用上下文和 secret redaction，不改变现有 URL 校验红线。
- `services/api/p2r_api/dependencies.py`、`services/api/p2r_api/main.py`、`services/api/p2r_api/routes/__init__.py`：注册 runtime secret store、依赖注入和新路由。
- `services/api/p2r_api/routes/papers.py`：扩展 TXT/粘贴文本入口，复用 Paper/PaperVersion/Artifact 完整校验；PDF 原有校验保持不变。
- `services/api/p2r_api/routes/experiments.py`：接收并校验 Provider 生成的候选，不允许客户端绕过 Recipe registry。
- `services/api/p2r_api/routes/runs.py`、`services/api/p2r_api/routes/reports.py`：要求 EnvironmentCheck 和显式 execution confirmation；禁止无 Run 生成实验结果报告。
- `services/api/p2r_api/demo.py`、`services/runner/p2r_runner.py`：保留 `offline_fixture` 回归路径，新增显式注册 Recipe 的注入点，不接受 shell/argv。
- `apps/web/src/api-client/guided.ts`、`guided-types.ts`：加入新 DTO、API paths 和校验器。
- `apps/web/src/features/guided/state.ts`、`controller.ts`、`GuidedPage.tsx`、`guided.css`：单列 Paper→Provider→Extraction→Experiment→Environment/Run→Report/Export 流程、动画和恢复。
- `apps/web/tests/guided-controller.test.mjs`、`guided-state.test.mjs`、`guided-api.test.mjs`、`guided-readback.test.mjs`：保留原 controlled-smoke 回归并补充新状态。
- `docs/verification/` 下新增本轮验证记录，不改写既有历史记录。

---

### Task 1: 建立文章抽取和实验方案的版本化契约

**Files:**
- Create: `packages/orchestration/p2r_orchestration/article_schema.py`
- Create: `tests/unit/phase02/test_article_schema.py`
- Modify: `packages/orchestration/p2r_orchestration/providers.py`

- [ ] **Step 1: 写失败测试，锁定字段、缺失值和支持级别**

在 `tests/unit/phase02/test_article_schema.py` 添加：

```python
from pydantic import ValidationError
from p2r_orchestration.article_schema import ExtractionDocument, ExperimentSpec


def test_extraction_keeps_missing_fields_and_source_refs():
    value = ExtractionDocument.model_validate({
        "schema_version": "article-extraction.v1",
        "title": {"value": None, "evidence_status": "missing", "source_refs": []},
        "task": {"value": "classification", "evidence_status": "supported", "source_refs": ["ref-1"]},
        "methods": [], "formulas": [], "datasets": [], "candidate_experiments": [],
        "warnings": [], "limitations": [],
    })
    assert value.title.value is None
    assert value.task.source_refs == ["ref-1"]


def test_experiment_rejects_unknown_support_status():
    try:
        ExperimentSpec.model_validate({
            "candidate_id": "x", "recipe_id": "r", "recipe_version": "1",
            "support_status": "maybe", "parameters": {}, "required_dependencies": [],
            "required_datasets": [], "resource_requirements": {}, "estimated_runtime": None,
            "source_refs": [],
        })
    except ValidationError:
        return
    raise AssertionError("unknown support status must be rejected")
```

- [ ] **Step 2: 运行定向测试，确认契约模块尚不存在**

Run: `cd '/Users/andyzhao/Documents/claude code/Ali_Hackthon/.worktrees/phase0-phase1' && PYTHONPATH=. .venv/bin/pytest tests/unit/phase02/test_article_schema.py -q`

Expected: FAIL because `p2r_orchestration.article_schema` is not implemented.

- [ ] **Step 3: 实现最小 Pydantic 契约**

在 `article_schema.py` 定义 frozen/extra-forbid 的 `EvidenceField`, `SourceRef`, `ExperimentSpec`, `ExtractionDocument`；`support_status` 只允许 `supported | review_required | unsupported`，`evidence_status` 只允许 `supported | inferred | missing | conflicting`。同时提供 `execution_eligible(spec)`，仅当 `support_status == 'supported'` 且 `recipe_id/version` 非空时返回真。

在 `providers.py` 的 `CompletionRequest` 增加可选 `response_schema_version` 元数据，调用层仍通过现有 `json_schema` 传 schema；禁止改变 `ProviderConfig.safe_base_url`、secret_env 校验和响应 secret 检查。

- [ ] **Step 4: 运行定向测试和现有 Provider 测试**

Run: `PYTHONPATH=. .venv/bin/pytest tests/unit/phase02/test_article_schema.py tests/unit/phase02/test_providers.py -q`

Expected: PASS；原 Provider URL、capability、invalid response 测试不回归。

---

### Task 2: 接入文章输入、Provider session 和运行时 secret

**Files:**
- Create: `packages/orchestration/p2r_orchestration/provider_sessions.py`
- Create: `services/api/p2r_api/routes/provider_sessions.py`
- Create: `tests/unit/phase02/test_provider_sessions.py`
- Create: `tests/integration/phase02/test_article_entry.py`
- Modify: `packages/persistence/p2r_persistence/models.py`
- Modify: `packages/persistence/alembic/versions/0015_article_provider_environment.py`
- Modify: `services/api/p2r_api/routes/papers.py`
- Modify: `services/api/p2r_api/dependencies.py`, `services/api/p2r_api/main.py`, `services/api/p2r_api/routes/__init__.py`

- [ ] **Step 1: 先写 secret 生命周期和入口失败测试**

必须覆盖：同一运行时 session 只返回安全投影；`repr()`/`model_dump()` 不包含 secret；clear 后不可调用；API Key 不进入数据库/Artifact；TXT、粘贴文本和 PDF 统一生成 `PaperVersion` 与 SHA-256；缺少 consent、超限和无效 MIME 返回安全错误。

- [ ] **Step 2: 运行测试确认新边界失败**

Run: `PYTHONPATH=. .venv/bin/pytest tests/unit/phase02/test_provider_sessions.py tests/integration/phase02/test_article_entry.py -q`

Expected: FAIL at missing runtime session and text-input routes.

- [ ] **Step 3: 实现 runtime-only secret store 和安全 Provider session API**

`ProviderSecretStore` 使用进程内字典，key 为不可预测 session id，value 不可序列化到 DTO；实现 `put`, `get`, `clear`, `public_summary`。路由只允许返回 `provider_session_id`, endpoint alias, model id, `secret_configured` 和掩码状态；所有写操作要求 `Idempotency-Key`。Provider endpoint 复用 `ProviderConfig` 进行 HTTPS/loopback/credentials/query/fragment 校验。

- [ ] **Step 4: 扩展 Paper 入口为 PDF/TXT/粘贴文本**

增加受限文本 payload（明确最大字节数、UTF-8 校验、非空校验、sha256 和 `media_type=text/plain`），保留 PDF magic/page/size/hash/artifact 校验。粘贴内容不在前端 localStorage 保存；服务端只保存 Artifact 和 PaperVersion。

- [ ] **Step 5: 注册路由并运行集成测试**

Run: `PYTHONPATH=. .venv/bin/pytest tests/unit/phase02/test_provider_sessions.py tests/integration/phase02/test_article_entry.py tests/integration/phase02/test_papers.py -q`

Expected: PASS；并验证 `git diff --check`。

---

### Task 3: 实现真实结构化抽取和候选 ExperimentSpec

**Files:**
- Modify: `services/api/p2r_api/routes/papers.py`
- Modify: `packages/orchestration/p2r_orchestration/providers.py`
- Modify: `packages/persistence/p2r_persistence/models.py`
- Modify: `services/api/p2r_api/routes/experiments.py`
- Create: `tests/integration/phase02/test_extraction_provider.py`

- [ ] **Step 1: 写 fake transport 测试**

使用注入的 `AsyncTransport` 返回合法 OpenAI-compatible JSON，断言：请求包含文章上下文、schema、prompt/policy version 和 call id；Extraction 保存 context hash/provider config/model/prompt version；返回 invalid JSON、错误 schema、refusal、包含 secret 的文本时拒绝且不落盘敏感内容。

- [ ] **Step 2: 运行测试确认当前 PDF literal-only 实现不满足**

Run: `PYTHONPATH=. .venv/bin/pytest tests/integration/phase02/test_extraction_provider.py -q`

Expected: FAIL，因为当前 `papers.py::_extract_pdf` 只做确定性 literal text extraction。

- [ ] **Step 3: 实现 Provider extraction service**

新增一个可注入的 extraction service：读取已校验 Paper Artifact，构造有限长度文章上下文，生成 `CompletionRequest`，调用 Provider，解析 JSON、通过 `ExtractionDocument` schema、校验每个 `source_ref` 只引用当前 Paper，最后生成不可变 Extraction 和 SourceRef。Provider 失败只保存安全错误码，不保存原始 response。

- [ ] **Step 4: 让 Experiment API 只接受后端可识别 Recipe**

Experiment route 对 `candidate_experiment_id`, `recipe_id`, `recipe_version`, 参数和 required resources 做白名单校验；客户端不能把 `recipe_id` 改成任意命令或传入 shell/argv。`supported` 之外仍可保存方案，但创建 Run 时必须拒绝。

- [ ] **Step 5: 运行抽取、Experiment 和旧全量后端测试**

Run: `PYTHONPATH=. .venv/bin/pytest tests/integration/phase02/test_extraction_provider.py tests/integration/phase02/test_experiments.py tests/unit/phase02 -q`

Expected: PASS；再运行现有后端全量测试并记录本轮结果，不把历史结果当作本轮证据。

---

### Task 4: 建立本机 EnvironmentCheck 和执行决策

**Files:**
- Create: `packages/orchestration/p2r_orchestration/environment_checker.py`
- Create: `services/api/p2r_api/routes/environment.py`
- Create: `tests/unit/phase02/test_environment_checker.py`
- Create: `tests/integration/phase02/test_environment_routes.py`
- Modify: `packages/persistence/p2r_persistence/models.py`
- Modify: `services/api/p2r_api/main.py`, `services/api/p2r_api/dependencies.py`, `services/api/p2r_api/routes/__init__.py`
- Modify: `packages/persistence/alembic/versions/0015_article_provider_environment.py`

- [ ] **Step 1: 写 fake probe 失败测试**

测试四种结果：所有必需项 pass → `ready`；任一 fail → `blocked`；关键项 unknown → `blocked`；recipe unsupported → `unsupported`。断言检查快照含 checker version、observed/required/suggestion，且检查器不执行修复命令。

- [ ] **Step 2: 运行定向测试确认模块缺失**

Run: `PYTHONPATH=. .venv/bin/pytest tests/unit/phase02/test_environment_checker.py tests/integration/phase02/test_environment_routes.py -q`

Expected: FAIL at missing checker/route.

- [ ] **Step 3: 实现可注入检查器**

定义 `EnvironmentProbe` 协议和默认只读 probe：platform/arch、Python、依赖 metadata、CPU、GPU/CUDA（不可用时 unknown）、memory、disk、dataset path/hash、network requirement。检查结果使用 `pass/fail/unknown/not_needed`，决策只把全部 required pass 视为 ready。

- [ ] **Step 4: 实现检查接口和持久化快照**

接口要求项目权限、Experiment/Revision 关联和幂等键；返回 `environment_check_id`, status, items, repair suggestions。GET 只读，不重新执行检查或创建 Run；重复写入同 key/body 返回相同 receipt。

- [ ] **Step 5: 验证权限、幂等和旧测试**

Run: `PYTHONPATH=. .venv/bin/pytest tests/unit/phase02/test_environment_checker.py tests/integration/phase02/test_environment_routes.py tests/integration/phase01 tests/integration/phase02 -q`

Expected: PASS；`unknown`、跨项目资源和无 Recipe 的 Run 创建均被拒绝。

---

### Task 5: 加入手动 SSH 验证和远程执行目标

**Files:**
- Create: `packages/orchestration/p2r_orchestration/ssh_executor.py`
- Modify: `services/api/p2r_api/routes/environment.py`
- Modify: `packages/persistence/p2r_persistence/models.py`
- Modify: `tests/unit/phase02/test_ssh_executor.py`
- Modify: `tests/integration/phase02/test_environment_routes.py`

- [ ] **Step 1: 写输入边界和验证状态测试**

覆盖 host/port/user/workdir schema；拒绝密码和私钥正文；只接受本机受控 key reference 或 agent id；验证成功才产生 `ssh_verification_id`; 失败不得产生 Run；不会自动从 blocked 转 SSH。

- [ ] **Step 2: 运行测试确认新 verifier 缺失**

Run: `PYTHONPATH=. .venv/bin/pytest tests/unit/phase02/test_ssh_executor.py -q`

Expected: FAIL at missing SSH verifier.

- [ ] **Step 3: 实现 `SSHExecutor` 协议和 fake implementation**

协议只接收结构化目标和注册 Recipe；验证步骤返回远程安全环境摘要和状态。实现不得暴露密码/私钥正文、不得接受任意 command/argv；真实 transport 留在后续受控适配，不在本阶段联网。

- [ ] **Step 4: 接入 SSH verify API 和显式 execution target**

`POST /environment-checks/{id}/ssh-verifications` 必须要求 Blocked 状态、幂等键和用户主动选择；成功后仍返回 `awaiting_confirmation`，不创建 Run。只有独立的 confirm endpoint 在 verification + environment + supported recipe 都满足时才能调用现有 Run creation。

- [ ] **Step 5: 运行后端全量边界测试**

Run: `PYTHONPATH=. .venv/bin/pytest tests/unit/phase02 tests/integration/phase02 tests/integration/phase01 -q`

Expected: PASS；git diff 中不得出现 secret fixture、私钥正文或凭据样例。

---

### Task 6: 收敛 Run/Report/Runner 的真实性边界

**Files:**
- Modify: `services/api/p2r_api/routes/runs.py`
- Modify: `services/api/p2r_api/routes/reports.py`
- Modify: `services/runner/p2r_runner.py`
- Modify: `services/api/p2r_api/demo.py`
- Modify: `tests/integration/phase02/test_environment_routes.py`
- Create: `tests/integration/phase02/test_execution_truth.py`

- [ ] **Step 1: 写失败测试**

断言：unsupported recipe、blocked local check、未验证 SSH、未确认 execution target 都不能创建 Run；没有成功 Run 时不能生成实验结果报告；成功 Run 报告包含 target、recipe version、environment snapshot、source refs、限制和 artifact metadata。

- [ ] **Step 2: 运行测试确认旧路由允许固定 demo 直达**

Run: `PYTHONPATH=. .venv/bin/pytest tests/integration/phase02/test_execution_truth.py -q`

Expected: FAIL on the new preconditions while existing offline demo regression remains separately green.

- [ ] **Step 3: 在 Run 创建前强制检查**

Run route 读取 ExperimentSpec、latest EnvironmentCheck、confirmation 和 optional SSH verification；仅 `supported + ready + explicit confirmation`（local）或 `supported + remote verification + explicit confirmation`（ssh）可创建 Run。保留 `create_demo_app` 的明确 fixture path，但标识为 controlled smoke，不把它当文章真实复现。

- [ ] **Step 4: 收紧 runner registry**

`p2r_runner.py` 暴露 Recipe registry 和 validated parameter dispatch；继续支持 `offline_fixture`，所有未知 recipe、shell、argv、任意工作目录调用抛出 `FixtureRunnerError`/安全业务错误。Run output 标记 target 和 evidence class。

- [ ] **Step 5: 运行完整后端回归**

Run: `PYTHONPATH=. .venv/bin/pytest -q`

Expected: PASS；如旧测试依赖固定 demo，应只在测试 harness 中显式传 controlled-smoke preconditions，不修改产品约束来迁就旧路径。

---

### Task 7: 接入单列向导的 Article/Provider/Environment/SSH UI

**Files:**
- Create: `apps/web/src/api-client/article-types.ts`
- Create: `apps/web/src/api-client/article.ts`
- Create: `apps/web/tests/article-entry.test.mjs`
- Create: `apps/web/tests/environment-flow.test.mjs`
- Modify: `apps/web/src/features/guided/GuidedPage.tsx`
- Modify: `apps/web/src/features/guided/controller.ts`
- Modify: `apps/web/src/features/guided/state.ts`
- Modify: `apps/web/src/features/guided/guided.css`
- Modify: `apps/web/src/api-client/guided.ts`, `guided-types.ts`
- Modify: `apps/web/tests/guided-controller.test.mjs`, `guided-state.test.mjs`, `guided-api.test.mjs`, `guided-readback.test.mjs`

- [ ] **Step 1: 写前端失败测试**

测试真实入口的最小交互模型：空状态只显示 Paper 输入；Provider 表单要求 endpoint/model/key；成功抽取后显示审核；Ready 显示“确认并开始本机实验”；Blocked 只显示缺失项、“改用 SSH”和“只保存方案”；Unsupported 不显示执行按钮；SSH 未验证前不显示确认执行。

- [ ] **Step 2: 运行前端定向测试确认新状态缺失**

Run: `npm test --prefix apps/web -- article-entry.test.mjs environment-flow.test.mjs`

Expected: FAIL，因为当前 GuidedPage 仅支持 consent + offline fixture。

- [ ] **Step 3: 扩展 DTO、state 和 controller**

增加 `paperInput`, `providerSession`, `extractionStatus`, `experimentSpec`, `environmentCheck`, `executionTarget`, `sshVerification`, `executionConfirmation` 状态；所有写方法保持 idempotency intent、receipt recovery 和 GET-only refresh。粘贴文章和 API Key 不进入 localStorage；刷新时只恢复安全资源引用，要求重新输入 secret。

- [ ] **Step 4: 重构 GuidedPage 为单列渐进式输入**

将当前 Paper 卡替换为文件/文本二选一入口；Provider 作为必要第二步，字段逐步展开。Extraction/Experiment 显示来源和支持级别。Environment/Run 根据 Ready/Blocked/Unsupported 分支渲染；SSH 表单只接受 host/port/user/key reference/agent id/workdir，不渲染密码输入，也不把 secret 写入 DOM 外的持久化。

- [ ] **Step 5: 保留和扩展视觉可访问性**

继续使用现有黑白圆角样式；增加 220–300ms 过渡、`prefers-reduced-motion`、焦点移动、`aria-live`、按钮禁用和真实 loading。动画结束不自动写 API，不以百分比伪造进度。

- [ ] **Step 6: 运行前端定向、全量测试和构建**

Run: `npm test --prefix apps/web`

Run: `npm run build --prefix apps/web`

Expected: 原有测试与新入口测试全部 PASS，Vite build 成功，无 API Key 出现在测试快照或 localStorage fixture。

---

### Task 8: 端到端验收、文档和回归证据

**Files:**
- Create: `docs/verification/2026-09-29-article-entry-environment-ssh.md`
- Modify: `docs/2026-09-27-paper2run-handoff.md`（仅追加本轮状态，不删除历史证据）

- [ ] **Step 1: 运行后端、前端和静态检查**

```bash
cd '/Users/andyzhao/Documents/claude code/Ali_Hackthon/.worktrees/phase0-phase1'
PYTHONPATH=. .venv/bin/pytest -q
npm test --prefix apps/web
npm run build --prefix apps/web
git diff --check
```

Expected: 全部通过；任何失败必须记录为当前阻断，不引用历史通过数。

- [ ] **Step 2: 启动本地 API 和 Web，执行空目录主线**

```bash
P2R_DEMO_DIR=.p2r-guided-verification/runtime \
PYTHONPATH=.:services/runner .venv/bin/python -m uvicorn \
  p2r_api.main:create_demo_app --factory --host 127.0.0.1 --port 8010

P2R_API_TARGET=http://127.0.0.1:8010 \
pnpm --dir apps/web dev --host 127.0.0.1 --port 5173 --strictPort
```

用浏览器验证：文章输入 → Provider fake/test path → extraction 审核 → experiment 确认 → environment Ready 或 Blocked → local confirm 或 SSH verify → report 状态。记录每个资源 ID 和真实状态，不把 UI 文案当作成功证据。

- [ ] **Step 3: 验证失败与刷新恢复**

至少验证：Provider auth/timeout/schema failure、缺 GPU/依赖导致 Blocked、SSH 验证失败、不支持 Recipe、重复点击、写后读取失败、刷新 GET-only、取消/失败 Run、无 Run 不生成结果报告。确认 API Key、私钥、Authorization header 不出现在日志、SSE、Artifact、报告、导出和浏览器存储。

- [ ] **Step 4: 记录验证报告**

在验证文档中记录范围、环境、命令、通过/失败/跳过项、浏览器截图路径、资源 ID、限制和未执行的下载落盘验收。明确标注 `local demo`、`controlled smoke`、`not full reproduction`。

- [ ] **Step 5: 最终检查工作区**

Run: `git status --short`

Expected: 只看到本轮明确新增/修改以及用户已有改动；不删除、不重置、不提交、不推送。

---

## 计划自审

- **规格覆盖：** 文章输入与 hash（Task 2）、Provider/schema/secret（Task 1–3）、Recipe 白名单（Task 3/6）、环境检查（Task 4）、SSH 手动分流（Task 5）、Run/Report 真实性（Task 6）、单列 UI/恢复（Task 7）、验收与文档（Task 8）均有对应任务。
- **安全覆盖：** secret runtime-only、URL 校验、无重定向、无任意命令、私钥正文拒绝、Artifact/Report 排除和日志扫描均有测试或验收步骤。
- **幂等覆盖：** Provider session、Paper、Extraction、EnvironmentCheck、SSH verify、Run、Report 都沿用现有 idempotency/receipt 语义；未知结果先 receipt，不自动重试。
- **占位符扫描：** 本计划无 `TODO`、`TBD` 或“稍后补充”等未决实施步骤；所有步骤包含路径、命令、预期结果或明确接口边界。
- **类型一致性：** `ExtractionDocument`/`ExperimentSpec` → EnvironmentCheck → Run precondition → Report metadata 的字段命名在任务间保持一致；前端 DTO 对应后端安全投影，不复用 secret 字段。
- **执行说明：** 这是计划，不修改运行时代码；按任务顺序实施，每个检查点先定向测试再进入下一任务；不执行 commit/push 以遵守当前工作区约束。
