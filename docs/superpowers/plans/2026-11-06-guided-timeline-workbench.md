# 引导式时间线工作台 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把「新建实验」长表单和任务详情合并成一条 9 步时间线的三栏工作台（无 App 导航侧栏），支持可续做、可删除的草稿。

**Architecture:** 前 4 步（论文 → 目标 → 模型 → 启动）的状态全部从现有服务端数据推出（草稿 = 没有任务的项目），推导逻辑是纯函数；启动后沿用 `stages.ts` 的事件归纳，由新的 `timeline.ts` 拼成 9 步。`#/new`、`#/draft/<projectId>` 由新 `DraftPage` 渲染，`#/task/<id>` 由改造后的 `TaskPage` 渲染，两者共用扩展后的 `Workbench`。后端只加 `DELETE /api/v1/projects/{id}`（软删除）并让 `GET /projects` 过滤已删除项目。

**Tech Stack:** React 18 + TypeScript + Vite（`apps/web`），`node --test` 跑 `.test.mjs`（直接 import `.ts`）；FastAPI + SQLAlchemy + pytest。

**Spec:** `docs/superpowers/specs/2026-11-06-guided-timeline-workbench-design.md`

## Global Constraints

- 路径：仓库根 `/Users/andyzhao/Documents/claude code/Ali_Hackthon`，分支 `feat/v3-integration`。前端命令在仓库根跑 `pnpm --dir apps/web test` / `pnpm --dir apps/web typecheck`；后端 `.venv/bin/python -m pytest`。
- `tests/unit/test_architecture.py` 与 `tests/unit/phase01/test_packaging.py` 任何时候都必须通过；不新增 Python 包。
- 界面文案全部中文。
- 新文件与改动文件中不得出现 `localStorage.` / `sessionStorage.`；API Key 不进 URL、不进浏览器存储，保存后立即清空输入框。
- 模型叙述（Pi 自报进度、专家建议）永不着 `ok` 色；只有工具结果与程序判定按现有规则着色。
- 9 步标题（逐字）：`上传论文`、`实验目标`、`连接模型`、`确认并启动`、`阅读与方案`、`编写与执行`、`冻结证据`、`候选与复核`、`报告`。第 4、5 步之间的分隔文案：`启动后由 AI 推进`。
- 删除的选项：科研专家、科研 Skills、实验协议 JSON、预算编辑、「校验配置」「保存配置」、项目选择/新建。
- 默认预算显示：`30 分钟 · 30 次模型调用 · 10 万 tokens · 3 轮修复`（按配置实际值格式化，见 Task 3 `budgetLine`）。
- 目标输入上限 2000 字。
- 目标探测只提示，不禁用启动。
- Conventional commits，结尾 `Co-Authored-By: Claude Code <noreply@anthropic.com>`。

## Review Focus

1. **应用重启后续做草稿**：模型连接已从进程内存消失 → 草稿停在「连接模型」，01/02 可点开查看，04 锁定；表单不能预填 base_url/模型（服务端不再有这些信息），但连接 ID 沿用配置里的 `provider_profile`。测试：Task 3 `draftProgress` 的 restart 用例 + `profileIdFor` 用例。
2. **上传失败留下空项目**：项目已建但论文没传上 → 总览里是「草稿 · 第 1 步」，可删除，不报错。测试：Task 3 无论文用例；Task 9 草稿行用例。
3. **配置版本冲突**（两个窗口同时编辑）：保存返回 409 `REVISION_CONFLICT` → 重新读取、重放本步改动、再存一次；第二次仍冲突才显示错误。测试：Task 3 `saveConfigWithRetry`。
4. **打开一个已经启动过的草稿链接**（另一个窗口点了启动）→ 跳到该项目最新任务，而不是再显示草稿。测试：Task 3 `newestTaskId` + Task 7 源码断言。
5. **删除有任务的项目** → 后端 409 `PROJECT_HAS_TASKS`，前端显示原因，列表不变。测试：Task 1 pytest + Task 2 客户端错误透传。

---

### Task 1: 后端删除草稿接口

**Files:**
- Modify: `services/api/p2r_api/routes/projects.py`（`ProjectService` 加方法；`list_projects` 过滤；新路由）
- Create: `tests/integration/phase02/test_project_delete_http.py`

**Interfaces:**
- Produces: `DELETE /api/v1/projects/{project_id}` → 204 空响应；错误：404（不存在/已删除/非成员，沿用 `require_project_access` 抛出的 `ResourceNotFoundError` 及其现有错误码），403（非 owner，`role_allows(role, "delete")`），409 `PROJECT_HAS_TASKS`。`GET /api/v1/projects` 不再返回 `deletion_state == "deleted"` 的项目。
- Produces: `ProjectService.delete_project(user_id: str, project_id: str) -> None`

- [ ] **Step 1: 写失败测试**（fixture 复制 `tests/integration/phase02/test_identity_http.py` 的 `setup`，用 `IdentityService(db).create_session(uid)` 拿 cookie，写请求带 `Origin: https://testserver` 与 `X-CSRF-Token`（`/api/v1/auth/me` 返回的 `csrf_token`））

```python
def test_delete_draft_project_hides_it(setup): ...
    # POST /api/v1/projects (Idempotency-Key) → id
    # DELETE /api/v1/projects/{id} → 204
    # GET /api/v1/projects → id not in [p["id"] for p in data]
    # GET /api/v1/projects/{id} → 404
    # DELETE again → 404
    # audit_logs has action == "project_deleted", resource_id == id

def test_delete_refuses_project_with_tasks(setup): ...
    # insert AutonomousTaskRecord(project_id=id, actor_id=uid, objective="o", config_snapshot={}) directly via factory()
    # DELETE → 409, json["error"]["code"] == "PROJECT_HAS_TASKS"; GET /projects still lists it

def test_delete_requires_csrf(setup): ...
    # DELETE without X-CSRF-Token → 403; project still listed
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m pytest tests/integration/phase02/test_project_delete_http.py -v`
Expected: FAIL（405 Method Not Allowed）

- [ ] **Step 3: 实现**
  - `delete_project`：`self.require_project(user_id, project_id, action="delete")`；用 `select(AutonomousTaskRecord.id).where(project_id==…).limit(1)`（`from p2r_persistence.autonomous import AutonomousTaskRecord`）判有任务 → `_error("PROJECT_HAS_TASKS", "project has autonomous tasks", 409)`；否则 `deletion_state="deleted"`、`version+1`、`updated_at=utcnow()`，加 `AuditLogRecord(action="project_deleted", resource_type="project", resource_id=project_id, details_json={"version": new_version})`，commit。
  - 路由：`@router.delete("/projects/{project_id}", status_code=204, response_class=Response)`，先 `_write_check(request, db, None, uid)`（只做 origin/CSRF），再 `delete_project`。不要求 Idempotency-Key（删除天然幂等）。
  - `list_projects` 查询加 `.where(ProjectRecord.deletion_state != "deleted")`。

- [ ] **Step 4: 运行通过 + 不变量**

Run: `.venv/bin/python -m pytest tests/integration/phase02 tests/unit/test_architecture.py tests/unit/phase01/test_packaging.py -q`
Expected: 全部 PASS

- [ ] **Step 5: Commit** `feat(api): soft-delete draft projects without tasks`

---

### Task 2: 前端项目 API（描述、读取、删除）

**Files:**
- Modify: `apps/web/src/api-client/papers.ts`
- Create: `apps/web/tests/papers-projects.test.mjs`

**Interfaces:**
- Produces（`ProjectInfo` 扩展，`PapersApi` 新方法）：

```ts
export interface ProjectInfo { id: string; name: string; description: string; version: number; role?: string; created_at?: string; updated_at?: string }
getProject(projectId: string, signal?: AbortSignal): Promise<ProjectInfo>            // GET /projects/{id}
updateProjectDescription(projectId: string, description: string, version: number): Promise<ProjectInfo>
  // PATCH /projects/{id}，headers: Content-Type json, Idempotency-Key 新 UUID, If-Match `"${version}"`；body {description}
deleteProject(projectId: string): Promise<void>                                       // DELETE /projects/{id}，期望 204
```
- `listProjects` 解析 `description`（缺省 `''`）、`version`（缺省 1）、`updated_at`。非 2xx 一律 `throw errorEnvelope(...)`，所以 409 `PROJECT_HAS_TASKS` / `REVISION_CONFLICT` 以 `ApiError.code` 透出。

- [ ] **Step 1: 写失败测试**（fake fetcher 记录 `url/method/headers/body`，仿 `tests/docparse-api.test.mjs`）
  - `updateProjectDescription('p', '目标', 3)` 发 `PATCH /api/v1/projects/p`，`If-Match` 为 `"3"`，有 `Idempotency-Key`，body `{"description":"目标"}`，返回解析出的 `version`。
  - `deleteProject('p')` 发 `DELETE`；204 → resolve；409 body `{error:{code:'PROJECT_HAS_TASKS',message:'x'}}` → reject，`err.code === 'PROJECT_HAS_TASKS'`。
  - `listProjects` 对缺 `description` 的行给 `''`。

- [ ] **Step 2:** Run `pnpm --dir apps/web test` → 新测试 FAIL（方法不存在）
- [ ] **Step 3:** 实现（复用文件内 `send` / `responsePayload` / `errorEnvelope` / `envelopeData`）
- [ ] **Step 4:** Run `pnpm --dir apps/web test && pnpm --dir apps/web typecheck` → PASS
- [ ] **Step 5: Commit** `feat(web): project description, read and delete client calls`

---

### Task 3: 草稿推导纯函数

**Files:**
- Create: `apps/web/src/features/flow/draft.ts`
- Create: `apps/web/tests/flow-draft.test.mjs`

**Interfaces:**
- Consumes: `ProjectConfig`、`ModelProfile`、`AutonomousTask`（`api-client/autonomous.ts`）；`PaperTextStatus`（`papers.ts`）；`defaultConfig`（`features/autonomous/config.ts`）。
- Produces:

```ts
export type SetupStepId = 'paper' | 'objective' | 'model' | 'launch';
export const SETUP_ORDER: SetupStepId[] = ['paper', 'objective', 'model', 'launch'];
export const OBJECTIVE_LIMIT = 2000;
export interface DraftPaper { paperId: string; versionId: string; title: string; pdf: boolean; textStatus: PaperTextStatus | null; pageCount: number | null }
export interface DraftFacts { paper: DraftPaper | null; description: string; config: ProjectConfig | null; profiles: ModelProfile[]; taskCount: number }
export interface DraftProgress { done: Record<SetupStepId, boolean>; current: SetupStepId; connectionLost: boolean; objective: string }
export function draftProgress(f: DraftFacts): DraftProgress
export function mergeConfig(saved: ProjectConfig | null, projectId: string, patch: { objective?: string; model?: ProjectConfig['model']; target?: ProjectConfig['target'] }): ProjectConfig
export function profileIdFor(projectId: string, config: ProjectConfig | null): string
export function objectiveError(text: string): string            // '' | '请填写实验目标' | '实验目标最多 2000 字'
export function budgetLine(limits: ProjectConfig['limits']): string
export function newestTaskId(tasks: Pick<AutonomousTask, 'id' | 'created_at' | 'updated_at'>[]): string | null
export function pickPaper(papers: DraftPaper[]): DraftPaper | null   // 最新一篇有 versionId 的；没有则 null
export async function saveConfigWithRetry(read: () => Promise<{ version: number; config: ProjectConfig } | null>, save: (config: ProjectConfig, version?: number) => Promise<{ version: number; config: ProjectConfig }>, apply: (saved: ProjectConfig | null) => ProjectConfig): Promise<{ version: number; config: ProjectConfig }>
```

规则（`draftProgress`）：
- `paper` 完成 = `paper && versionId && (!pdf || textStatus === 'ready')`
- `objective` 取 `config?.experiment.objective.trim() || description.trim()`；非空即完成
- `model` 完成 = `config?.model.provider_profile` 存在，且 `profiles` 里同 id 的连接 `secret_configured === true || auth_mode === 'none'`
- `launch` 完成 = `taskCount > 0`
- `current` = `SETUP_ORDER` 中第一个未完成项（全完成为 `'launch'`）
- `connectionLost` = 配置里有 `provider_profile` 但 `profiles` 中找不到该 id
- `mergeConfig`：以 `saved ?? defaultConfig(projectId)` 为底，只覆盖 patch 中给出的键（objective 写入 `experiment.objective`），其余字段（specialists、harness.skills、limits、target 等）原样保留
- `profileIdFor`：配置里已有 `provider_profile` 则返回它；否则 `model-${projectId.replace(/[^A-Za-z0-9]/g, '').slice(0, 12)}`（须匹配 `^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$`）
- `budgetLine`：`${Math.round(wall_time_seconds/60)} 分钟 · ${max_model_calls} 次模型调用 · ${tokens} tokens · ${max_repair_rounds ?? 3} 轮修复`，tokens 为整万时写 `N 万`，否则写原数字
- `saveConfigWithRetry`：`read` → `save(apply(saved?.config ?? null), saved?.version)`；若抛出 `ApiError` 且 `code === 'REVISION_CONFLICT'`，再 `read` + `save` 一次；第二次仍失败则抛出

- [ ] **Step 1: 写失败测试**（`flow-draft.test.mjs`），至少这些断言：
  - 全新 `{paper:null, description:'', config:null, profiles:[], taskCount:0}` → `current 'paper'`，`connectionLost false`
  - PDF `textStatus:'parsing'` → `current 'paper'`；粘贴正文 `pdf:false, textStatus:'none'` → `done.paper true`
  - 只有描述 `'复现表 2'`、无配置 → `current 'model'`，`objective '复现表 2'`
  - 配置 `provider_profile:'model-abc'`、`profiles:[]` → `current 'model'`，`connectionLost true`，`done.paper/objective true`
  - 连接存在 `secret_configured:true` → `current 'launch'`；`taskCount:1` → `done.launch true`
  - `mergeConfig(v3Config /* 有 specialists 1 个、skills ['s1']、limits.wall_time_seconds 600 */, 'p', {objective:'新'})` 保留 specialists、skills、600，`experiment.objective === '新'`
  - `profileIdFor('3f2a-9c…', null)` 匹配名称正则；`profileIdFor('p', cfgWith('keep-me'))` === `'keep-me'`
  - `objectiveError('')`、`objectiveError('x'.repeat(2001))`、`objectiveError('ok') === ''`
  - `budgetLine(defaultConfig('p').limits) === '30 分钟 · 30 次模型调用 · 10 万 tokens · 3 轮修复'`
  - `newestTaskId` 取 `updated_at`（缺失用 `created_at`）最大者；空数组 → `null`
  - `saveConfigWithRetry`：第一次 save 抛 `new ApiError('x', 409, 'REVISION_CONFLICT')`，第二次成功 → 返回第二次结果且 `read` 调用 2 次；连续两次冲突 → reject；其他错误码不重试

- [ ] **Step 2:** Run `pnpm --dir apps/web test` → FAIL
- [ ] **Step 3:** 实现 `draft.ts`（纯模块，不 import React）
- [ ] **Step 4:** Run `pnpm --dir apps/web test && pnpm --dir apps/web typecheck` → PASS
- [ ] **Step 5: Commit** `feat(web): derive draft progress from server state`

---

### Task 4: 9 步时间线（嵌套进度 + 分组）

**Files:**
- Modify: `apps/web/src/ui/workbench/Workbench.tsx`（只改类型：`WbStageItem.children?: WbStageItem[]`，`WbStage.group?: string`）
- Modify: `apps/web/src/features/autonomous/stages.ts`（`buildStages` 产出嵌套条目）
- Create: `apps/web/src/features/flow/timeline.ts`
- Modify: `apps/web/tests/autonomous-timeline.test.mjs`（新增用例）
- Create: `apps/web/tests/flow-timeline.test.mjs`

**Interfaces:**
- Consumes: `buildStages`（stages.ts），Task 3 的 `SetupStepId`。
- Produces:

```ts
// timeline.ts
export const SETUP_TITLES: Record<SetupStepId, string> = { paper: '上传论文', objective: '实验目标', model: '连接模型', launch: '确认并启动' };
export const RUN_TITLES = { plan: '阅读与方案', execute: '编写与执行', evidence: '冻结证据', review: '候选与复核', report: '报告' } as const;
export const AI_GROUP = '启动后由 AI 推进';
export interface SetupView { id: SetupStepId; done: boolean; summary: string | null }
export function flowStages(input: { setup: SetupView[]; current: SetupStepId | null; task: AutonomousTask | null; events: AutonomousEvent[] }): { stages: WbStage[]; activeId: string; doneCount: number }
```

`buildStages` 嵌套规则：在某阶段内，`progress_noted` 推入一级条目 `{ id: 'n<seq>', label: summary || phase || 'Pi 自报进度', detail: 'Pi 自报（模型叙述）', tone: 'idle', children: [] }`；之后同阶段的工具调用、事件条目推入它的 `children`；阶段切换或下一条自报时结束该组。没有自报之前的条目保持一级。`slice(-14)` 只作用于一级条目。工具完成着色逻辑不变（对象引用不变，嵌套后仍能更新）。

`flowStages` 规则：
- 前 4 步：`task` 存在时全部 `mark:'done'`；否则 `done → 'done'`，`id === current → 'current'`，其余 `'locked'`。`disabled = mark === 'locked'`。`summary` 用 `SetupView.summary`。
- `buildStages(task, events)` 的 `setup` 阶段不单独显示：它的 `items` 并入 `launch` 步。其余 5 阶段按 `RUN_TITLES` 改标题，保留 mark/summary/items；`plan` 阶段 `group: AI_GROUP`。无任务时 5 阶段 `mark:'locked'`、`items: []`、summary 用 `PHASES` 的 `idle`。
- `activeId`：无任务 → `current`；有任务 → `buildStages().active`，若为 `'setup'` 则 `'launch'`。
- `doneCount` = mark 为 `done` 的步数。

- [ ] **Step 1: 写失败测试**
  - autonomous-timeline：事件 `progress_noted{phase:'读代码',summary:'先核对模型配置'}` → `tool_started read` → `tool_completed` 后，`plan.items[0].label === '先核对模型配置'`，`tone === 'idle'`，`children[0].label` 以 `读取` 开头且 `tone === 'ok'`；自报之前的工具调用仍是一级；现有 `workbench.test.mjs` 的 `setup/plan/execute` 断言不改仍通过。
  - flow-timeline：无任务、`current:'model'` → 9 个 stage，标题依次等于 Global Constraints 的 9 个标题；mark 依次 `done,done,current,locked×6`；`stages[4].group === '启动后由 AI 推进'`；`activeId === 'model'`。
  - 有任务（事件同 workbench.test.mjs 第一个用例）→ 前 4 步 `done`；`launch` 的 items 含「任务已创建」；`activeId === 'execute'`；任何 `detail` 含「模型叙述」的条目 `tone !== 'ok'`。

- [ ] **Step 2:** Run `pnpm --dir apps/web test` → 新用例 FAIL
- [ ] **Step 3:** 实现
- [ ] **Step 4:** Run `pnpm --dir apps/web test && pnpm --dir apps/web typecheck` → PASS
- [ ] **Step 5: Commit** `feat(web): nine-step flow timeline with nested progress notes`

---

### Task 5: Workbench 外框（左栏顶、分组、二级条目）与 errorDetail 迁出

**Files:**
- Modify: `apps/web/src/ui/workbench/Workbench.tsx`、`apps/web/src/ui/workbench/workbench.css`
- Create: `apps/web/src/features/autonomous/errors.ts`（迁入 `errorDetail`，原样）
- Modify: `apps/web/src/features/autonomous/TaskPage.tsx`（改为 `import { errorDetail } from './errors'` 并 `export { errorDetail }` 以兼容本任务内其他引用）、`apps/web/tests/helpers/error-detail.mjs`（改为直接 `export { errorDetail } from '../../src/features/autonomous/errors.ts'`）
- Test: `apps/web/tests/workbench.test.mjs`

**Interfaces:**
- Produces（`WorkbenchProps` 新增）：`railHead?: ReactNode`。提供时进入「应用布局」：左栏贯穿全高，顶部渲染 `railHead`，`wb-head`（标题/状态/meta/actions）移到中、右两栏上方；未提供时布局与现在完全一致。
- 左栏：`stage.group` 与上一 stage 不同时，在其前渲染 `<li className="wb-group" role="presentation">{group}</li>`。
- 二级条目：有 `children` 的条目渲染为可展开行（默认收起，`aria-expanded`），展开后在其下缩进列出 children，children 复用现有 `wb-sub` 渲染（含 `openPath` 打开文件）。
- CSS：`.wb.is-app` 用 grid areas `"rail head head" "rail viewer files"`，列宽沿用 `260px minmax(0,1fr) 300px` 及现有断点；`[data-shell="darwin"] .wb.is-app .wb-rail-head` 左侧留 82px 给红绿灯并带 `drag` 区（仿 `shell.css` 第 71 行）。

- [ ] **Step 1: 写失败测试**（workbench.test.mjs 源码断言）：`Workbench.tsx` 含 `railHead`、`wb-group`、`aria-expanded` 用于条目展开；`errors.ts` 导出 `errorDetail`；`autonomous-outcomes` 的 errorDetail 用例经新 helper 仍通过。
- [ ] **Step 2:** Run `pnpm --dir apps/web test` → FAIL
- [ ] **Step 3:** 实现
- [ ] **Step 4:** Run `pnpm --dir apps/web test && pnpm --dir apps/web typecheck && pnpm --dir apps/web build` → PASS
- [ ] **Step 5: Commit** `feat(web): workbench app layout with rail head, groups and nested items`

---

### Task 6: 路由与外壳

**Files:**
- Create: `apps/web/src/shell/routes.ts`
- Modify: `apps/web/src/shell/AppShell.tsx`
- Create: `apps/web/tests/routes.test.mjs`
- Modify: `apps/web/tests/autonomous-entry.test.mjs`（shell 断言改读 `routes.ts`）

**Interfaces:**
- Produces:

```ts
export type Route =
  | { view: 'overview' } | { view: 'settings' }
  | { view: 'new' } | { view: 'draft'; projectId: string } | { view: 'task'; taskId: string };
export function parseRoute(hash: string): { route: Route; redirect: string | null }
export const draftHref = (projectId: string) => `#/draft/${encodeURIComponent(projectId)}`;
```
- 规则：`#/autonomous?task=<id>` → redirect `#/task/<id>`；`#/autonomous` → `#/new`；`#/new?project=<id>` → `#/draft/<id>`；`#/draft/<id>`、`#/task/<id>` 解码 id；其余同现状。
- `AppShell`：`new/draft/task` 渲染全幅（不渲染 `Sidebar` 和 `main-head`），分别挂 `DraftPage`（Task 7，`key` 取 `projectId ?? 'new'`）与 `TaskPage`；`overview/settings` 保持现有外壳。有 redirect 时 `window.location.replace(redirect)`。去掉「打开过就常驻挂载」逻辑（状态都在服务端）。本任务内 `DraftPage` 尚不存在时先挂现有 `NewExperimentPage` 占位，Task 7 替换。

- [ ] **Step 1: 写失败测试**（routes.test.mjs）覆盖上述 5 条映射与 id 解码（含 `%2F`）；autonomous-entry 断言 `routes.ts` 含 `#/autonomous` 处理、`AppShell.tsx` 含 `location.replace`。
- [ ] **Step 2:** Run `pnpm --dir apps/web test` → FAIL
- [ ] **Step 3:** 实现
- [ ] **Step 4:** Run `pnpm --dir apps/web test && pnpm --dir apps/web typecheck` → PASS
- [ ] **Step 5: Commit** `feat(web): full-bleed flow routes with draft links`

---

### Task 7: 草稿页与前 4 步

**Files:**
- Create: `apps/web/src/features/flow/DraftPage.tsx`（数据加载、Workbench 装配、步骤切换）
- Create: `apps/web/src/features/flow/StepFrame.tsx`、`steps/PaperStep.tsx`、`steps/ObjectiveStep.tsx`、`steps/ModelStep.tsx`、`steps/LaunchStep.tsx`、`flow.css`
- Create: `apps/web/src/features/flow/RailHead.tsx`（品牌 `Paper2Run` + `← 总览` 链接 `#/`；DraftPage 与 TaskPage 共用）
- Delete: `apps/web/src/features/autonomous/NewExperimentPage.tsx`
- Modify: `apps/web/src/shell/AppShell.tsx`（挂 DraftPage）
- Modify tests: `autonomous-entry.test.mjs`、`autonomous-specialists.test.mjs`、`autonomous-outcomes.test.mjs`、`theme.test.mjs`（见 Step 1）

**Interfaces:**
- Consumes: Task 2 API、Task 3 全部函数、Task 4 `flowStages`、Task 5 `railHead`、现有 `launchBlockReason`/`paperIsPdf`（launch.ts）、`ParseProgress`、`ModeDialog`、`canParseNow`、`TargetProbeView`/`parseProbe`/`probeMissingNotice`、`profileRequest`/`modelReference`、`errorDetail`（errors.ts）。
- Produces: `export default function DraftPage(props: { projectId: string | null; api?: AutonomousApi; papers?: PapersApi })`；`StepFrame({ guide: string; feedback: { tone: 'ok' | 'bad' | 'idle'; text: string } | null; children; next?: { label: string; disabled?: string; onClick(): void } })`。

行为：
- 加载（`projectId` 非空）：`papers.getProject`、`papers.listPapers` → 每篇 `getPaper`（+ PDF 时 `getPaperText`）→ `pickPaper`、`api.config`（404 → null）、`api.profiles()`、`api.tasks(projectId)`。若 `tasks.length > 0` → `location.replace('#/task/' + newestTaskId(tasks))`。`viewing` 初始为 `draftProgress().current`；点已完成/当前步可切换，锁定步不可点。
- 中栏 = `StepFrame`：guide（每步一句，见下）、feedback（上一步结果）、本步表单、下一步按钮；顶部错误用 `errorDetail`。
- **01 上传论文**：PDF 文件选择 + 「上传并解析」；`没有 PDF？粘贴正文` 展开项（标题可选 + 正文）。`projectId` 为 null 时先 `papers.createProject(名称)`（名称 = 文件名去扩展名或标题提示或 `未命名论文`，截到 120 字），成功后上传，再 `location.replace(draftHref(id))`。PDF 走现有解析模式对话框/解析进度/重试（逻辑从旧 NewExperimentPage 搬来，parse 调用带 `crypto.randomUUID()`）。完成反馈：`论文解析完成，共 N 页` / 失败原因。guide：`选择要复现的论文 PDF，或直接粘贴正文。`
- **02 实验目标**：textarea `maxLength={OBJECTIVE_LIMIT}` + 字数 `n / 2000`；示例 placeholder；「下一步」→ `objectiveError` 为空才可点 → `updateProjectDescription`；若已有配置，再 `saveConfigWithRetry(..., s => mergeConfig(s, id, { objective }))`。guide：`写清要验证什么、看哪个指标、允许多大范围。`
- **03 连接模型**：当前进程有 `secret_configured` 的连接时给下拉「使用已保存的连接」（含「新建连接」）；新建只填 服务地址、模型名称、API Key，外加 `OpenAI 兼容 / Anthropic` 两项（`api`）。`connectionLost` 时顶部提示 `应用重启后模型连接已清空，请重新填写。`。保存：`api.saveProfile(profileRequest({ id: profileIdFor(id, config), base_url, model, api, auth_mode: 'api-key' }, key, '', profiles))` → 立刻 `setKey('')` → `saveConfigWithRetry(..., s => mergeConfig(s, id, { model: modelReference(saved), objective: progress.objective }))`。反馈：`已保存。实际连接在启动时由服务端检查。` guide：`AI 用这个模型阅读论文、写代码、推进实验。`
- **04 确认并启动**：只读摘要（论文标题、目标前 120 字、`连接 · 模型名`）；执行位置行：`本地隔离环境` + 「使用 SSH」展开（SSH 配置别名、远端目录，默认 `/workspace/paper2run/<projectId>`），切换或修改后「保存执行位置」→ `saveConfigWithRetry(mergeConfig(..., { target }))` → 重新探测；`TargetProbeView` 显示探测结果，`probeMissingNotice` 作为提示文字，不进 `launchBlockReason`；预算行 `预算：${budgetLine(config.limits)}`；授权开关（文案沿用旧页 `我授权在上述目标与预算内联网、获取数据与代码、安装依赖和运行实验`）；`授权并启动` 按钮 `disabled={!!startBlockReason}`，旁边显示原因；点击 → `api.start(id, { objective, paper_version_id, authorization: { confirmed: true } })` → `location.replace('#/task/' + created.id)`。
- 右栏文件（启动前）：`paper/preview.md`（`PaperText.preview` 非空时，badge `预览`，内容即 preview 文本）与 `project/config.json`（有配置时，`JSON.stringify(config, null, 2)`，badge `记录`），由 `loadFile` 返回 `{ text }`。
- 草稿页标题区：title = 论文标题或 `新建实验`；status `草稿 · 第 N 步`；actions 含 `删除草稿`（`window.confirm('删除这个草稿？论文和配置将不再显示。')` → `papers.deleteProject` → `location.hash = '#/'`）。

- [ ] **Step 1: 改写源码不变量测试指向新文件**（先改测试再删旧页）：
  - autonomous-entry「new-experiment page owns…」改为读 `steps/PaperStep.tsx` + `steps/LaunchStep.tsx` + `DraftPage.tsx` 拼接文本，断言 `ModeDialog`、`ParseProgress`、`paper_version_id`、`授权并启动`、`launchBlockReason\(`、`paperIsPdf\(`、`startBlockReason`、`crypto\.randomUUID\(\)`，且无 `localStorage|sessionStorage`；并断言 DraftPage 含 `newestTaskId(`。
  - autonomous-specialists「never persists key…」：文件列表换成 `features/flow/**/*.tsx` + `TaskPage.tsx`；`ModelStep.tsx` 含 `setKey('')`；删除「专家输出仅建议」「共享总预算」两条断言（专家 UI 已按设计删除）。
  - autonomous-outcomes「emptied repair-round field」：保留 `validateConfig` 部分，删除读取 NewExperimentPage 的断言（预算编辑已删除）。
  - theme.test 存储清单：把 `NewExperimentPage.tsx` 换成 `features/flow/DraftPage.tsx` 及 4 个 step 文件。
  - 新增：`flow.css`/各 step 文件不含 `专家|Skills|协议 JSON|校验配置|保存配置`（删除项不回流，反目标 3）。
- [ ] **Step 2:** Run `pnpm --dir apps/web test` → 新断言 FAIL
- [ ] **Step 3:** 实现上述文件，删除 `NewExperimentPage.tsx`，AppShell 挂 `DraftPage`
- [ ] **Step 4:** Run `pnpm --dir apps/web test && pnpm --dir apps/web typecheck && pnpm --dir apps/web build` → PASS
- [ ] **Step 5: Commit** `feat(web): guided draft steps replace the long new-experiment form`（正文列出改写/删除的旧测试断言及原因）

---

### Task 8: 任务页接入时间线

**Files:**
- Modify: `apps/web/src/features/autonomous/TaskPage.tsx`
- Modify tests: `autonomous-isolation.test.mjs`（仅在结构变化导致桩树查找失败时调整查找方式，断言语义不变）、`autonomous-timeline.test.mjs`、`workbench.test.mjs`

**Interfaces:**
- Consumes: `flowStages`、`RailHead`、`errorDetail`（errors.ts，删去 TaskPage 中的兼容 re-export）。
- 改动：
  - `stages`/`activeStageId`/`progress` 改用 `flowStages({ setup, current: null, task, events })`；`setup` 摘要只来自 `task.config_snapshot`：paper → null；objective → 目标前 60 字；model → `provider_profile`；launch → 执行位置 `本地隔离环境` 或 `SSH · <ssh_config_alias>`。progress 文案 `进度 · ${doneCount} / 9`，保留 `按事件归纳` 徽章及其 title 原文 `阶段由 Pi 自报进度（note_progress）与事件归纳得出，不是程序判定`。
  - 传 `railHead={<RailHead />}`。
  - 删除「项目任务」切换下拉与「新建实验」链接（`auto-taskbar`）。
  - 实时区顶部状态卡（`auto-live-status`）：`运行中 · 第 N / 9 步 · <阶段标题>`、`正在：<最新 progress_noted 的 summary>`（无则省略）、`<tool_started 数> 步操作`；其下顺序：安全终止/三态提示 → 恢复卡 → 审批卡 → 错误 → 控制按钮 → 追加指令 → 候选卡 → 报告。`ProgressNotes`、`UsageView`、`SpecialistCalls`、Skills 快照、配置快照、原始事件流收进一个 `Disclosure variant="card" title="详细记录"`（`ProgressNotes`/`SpecialistCalls` 仍以 `<ProgressNotes`、`<SpecialistCalls` 形式出现在源码中）。
  - 所有现有不变量保留：显式批准/拒绝、`recovery_required` → `需要恢复`、暂停/停止不停远端计费的文案、`ReportView`、`UsageView`、`<Workbench`。

- [ ] **Step 1: 写失败测试**：workbench.test 断言 TaskPage 含 `flowStages(` 与 `railHead=`，不含 `auto-taskbar`；timeline 测试原有 TaskPage 断言保持。
- [ ] **Step 2:** Run `pnpm --dir apps/web test` → FAIL
- [ ] **Step 3:** 实现；跑 isolation 测试，若桩树结构变化导致查找失败，只改查找路径
- [ ] **Step 4:** Run `pnpm --dir apps/web test && pnpm --dir apps/web typecheck && pnpm --dir apps/web build` → PASS
- [ ] **Step 5: Commit** `feat(web): task page continues the nine-step timeline`

---

### Task 9: 总览列出草稿

**Files:**
- Modify: `apps/web/src/shell/hooks.ts`、`apps/web/src/features/overview/OverviewPage.tsx`
- Create: `apps/web/src/features/flow/draftData.ts`
- Create: `apps/web/tests/overview-drafts.test.mjs`

**Interfaces:**
- Produces:

```ts
// hooks.ts
export interface RecentDraft { projectId: string; name: string; updatedAt: string }
export async function loadAutonomousTasks(...): Promise<{ tasks: RecentTask[]; drafts: RecentDraft[]; failedProjects: number }>
  // 读取成功且任务数为 0 的项目进入 drafts，按 updatedAt 倒序
// draftData.ts
export async function loadDraftFacts(api: AutonomousApi, papers: PapersApi, projectId: string, profiles: ModelProfile[], signal?: AbortSignal): Promise<DraftFacts>
```
- DraftPage（Task 7）改用 `loadDraftFacts`（本任务内顺手替换，保持行为）。
- OverviewPage：筛选加 `{ value: 'draft', label: '草稿' }`；列表先列草稿行（tile 图标 `doc`，标题 = 项目名，副标题 `草稿 · 第 N 步 · <步骤标题>`，N 由 `draftProgress(await loadDraftFacts(...)).current` 得出，加载中显示 `草稿`），行尾 `删除` 按钮（`confirm` → `deleteProject` → 刷新；错误以 `errorDetail` 显示在页顶 callout）；行链接 `draftHref`。工作区卡副标题追加 `· N 个草稿`。空状态仅在任务与草稿都为 0 时出现。

- [ ] **Step 1: 写失败测试**：fake api/papers 两个项目（一个有任务、一个无）→ `drafts` 只含后者；`listPapers` 为空的草稿 → `draftProgress(loadDraftFacts(...)).current === 'paper'`；OverviewPage 源码含 `'draft'` 筛选值与 `deleteProject(`。
- [ ] **Step 2:** Run `pnpm --dir apps/web test` → FAIL
- [ ] **Step 3:** 实现
- [ ] **Step 4:** Run `pnpm --dir apps/web test && pnpm --dir apps/web typecheck && pnpm --dir apps/web build` → PASS
- [ ] **Step 5: Commit** `feat(web): list, resume and delete drafts from the overview`

---

### Task 10: 端到端验证

**Files:** 无代码改动（发现问题回到对应任务修）。截图存 `/tmp/p2r-shots/flow-*.png`，不入库。

- [ ] **Step 1:** 全量：`pnpm --dir apps/web test && pnpm --dir apps/web build` 与 `.venv/bin/python -m pytest -q`，记录通过数。
- [ ] **Step 2:** 用临时数据目录起服务（不占 8010）：`P2R_DEMO_DIR=/tmp/p2r-flow-data P2R_ALLOWED_ORIGINS=http://127.0.0.1:5180,http://localhost:5180 .venv/bin/python -m uvicorn p2r_api.main:create_local_app --factory --host 127.0.0.1 --port 8020` + `P2R_API_TARGET=http://127.0.0.1:8020 pnpm --dir apps/web dev --host 127.0.0.1 --port 5180 --strictPort`。
- [ ] **Step 3:** Playwright（`/opt/homebrew/opt/python@3.14/bin/python3.14 -I`，`executable_path` 指向 `~/Library/Caches/ms-playwright/chromium_headless_shell-1248/*/chrome-headless-shell`）走查并截图：`#/new` 无导航侧栏、9 步可见、5–9 锁定 → 粘贴正文提交 → URL 变 `#/draft/<id>`、第 1 步绿勾 → 填目标下一步 → 回 `#/` 看到 `草稿 · 第 3 步` → 点回草稿续做 → 填一个假连接（`http://127.0.0.1:9/v1`、`fake-model`、key `x`）→ 第 4 步显示预算行 `30 分钟 · 30 次模型调用 · 10 万 tokens · 3 轮修复` 与探测结果 → 授权启动 → URL 变 `#/task/<id>`、前 4 步全绿、时间线继续（预期模型调用失败，任务显示失败原因，失败不着绿）。
- [ ] **Step 4:** 重启 API 进程后打开该草稿的另一份副本（新建第二个草稿走到第 3 步再重启）→ 停在「连接模型」并显示 `应用重启后模型连接已清空，请重新填写。`，01/02 可点开。
- [ ] **Step 5:** 总览删除草稿 → 消失；对有任务的项目调用 `DELETE` → 409（curl 验证）。
- [ ] **Step 6:** 汇报：截图路径、各项结果、任何未通过项原样列出。
