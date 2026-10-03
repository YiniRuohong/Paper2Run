# Phase 0/1 Demo 验证记录

日期：2026-09-27
工作区：`/Users/andyzhao/Documents/claude code/Ali_Hackthon/.worktrees/phase0-phase1`
分支：`feat/paper2run-phase0-phase1`

## 本轮变更

- 修复 Artifact 文本读取的 UTF-8 字节游标处理：请求落在多字节字符中间时，服务端对齐到后续合法边界，并继续以字节 offset 返回 `offset` / `next_offset`。
- 保留首个 UTF-8 code point 内部非法 offset 的明确 `VALIDATION_ERROR` 行为。
- 保留 Attempt manifest 文件的 hash、bytes、Store 内容和项目权限校验。

## 验证命令与结果

| 命令 | 结果 |
|---|---|
| `PYTHONPATH=. .venv/bin/pytest -q` | 264 passed, 1 warning |
| `PYTHONPATH=. .venv/bin/python -m compileall -q services packages tests` | 通过 |
| `git diff --check` | 通过 |
| `pnpm --dir apps/web test` | 22 passed |
| `pnpm --dir apps/web exec tsc --noEmit` | 通过 |
| `pnpm --dir apps/web build` | 通过，Vite 产出 `apps/web/dist/index.html` 和 bundle |
| 本地 API + Vite `/lab` smoke | `LIVE_DEMO_SMOKE=passed` |

## Demo 启动

```bash
mkdir -p .p2r-demo-8010
P2R_DEMO_DIR=.p2r-demo-8010 PYTHONPATH=. .venv/bin/python -m uvicorn p2r_api.main:create_demo_app --factory --host 127.0.0.1 --port 8010
P2R_API_TARGET=http://127.0.0.1:8010 pnpm --dir apps/web dev --host 127.0.0.1 --port 5173
```

浏览器入口：`http://127.0.0.1:5173/lab`

## 已验证范围

- `/healthz` 返回 `status=ok`。
- `/lab` 可由 Vite 提供并加载前端入口。
- Session、Cell、Events、Actions、Checkpoint、Restore 使用真实本地 API。
- Attempt 文件读取只返回可验证的 Artifact。
- Artifact 文本读取支持严格 UTF-8 解码和字节游标续读。
- 前端写请求携带 `Idempotency-Key`，不会因后续刷新失败自动重试。

## 明确限制

当前 Demo 仍是本地 Phase 0/1 开发演示：使用 SQLite、本地 ArtifactStore 和受控本地 Python Kernel。它不提供 hostile-code sandbox、真实模型/GPU/外部 Runner、论文解析、真实训练、完整 Report、PostgreSQL 多 Worker 生产并发验证或公网部署。不能把 Demo 结果描述为完整论文复现或生产安全证明。

## 2026-09-28 追加：/api/v1/demo 404 阻断排查与修复

### 根因（两层）

1. 历史 404：`.p2r-demo-8010/server.log` 显示当时 8010 上的服务是旧代码进程（`/api/v1/demo` 未挂载），该进程后来已正常关闭（日志含 "Shutting down"）。404 与当前代码无关。
2. 真正阻断：`.p2r-guided-verification/api.log`（2026-09-28 02:01 启动尝试）显示 uvicorn 启动即崩溃 —— `p2r_api/routes/runs.py:19` 的 `from p2r_runner import run_fixture, FixtureRunnerError` 抛 `ModuleNotFoundError: No module named 'p2r_runner'`。

原因：`p2r_runner` 与 `p2r_runner_service` 是 `services/runner/` 下的单文件模块（无 `__init__.py`），`pyproject.toml` 未在 `[tool.setuptools]` 声明 `py-modules`，而 `packages.find.include` 只匹配包目录，因此 editable install 的 finder MAPPING 静默缺少这两个模块。pytest 因 `[tool.pytest.ini_options] pythonpath` 显式包含 `services/runner` 而全部通过，掩盖了安装缺陷；uvicorn 不走 pytest pythonpath，故启动失败。

### 修复

- `pyproject.toml`：新增 `[tool.setuptools] py-modules = ["p2r_runner", "p2r_runner_service"]`，并在 `[tool.setuptools.package-dir]` 补两条目录映射。已添加回归测试 `tests/unit/phase01/test_packaging.py`（修复前 2 failed，修复后 2 passed）。
- 注意：已存在的 `.venv` editable finder 不会因 pyproject 修改而自动更新；在重新 `pip install -e .` 之前，按下方命令以 `PYTHONPATH=.:services/runner` 启动（与 pytest pythonpath 同源，非新增依赖）。

### Loopback 全链路验证（2026-09-28，真实 HTTP，端口 8010）

启动：`P2R_DEMO_DIR=.p2r-loopback-fix PYTHONPATH=.:services/runner .venv/bin/python -m uvicorn p2r_api.main:create_demo_app --factory --host 127.0.0.1 --port 8010`

| 步骤 | 结果 |
|---|---|
| `GET /healthz` | 200 |
| `GET /api/v1/demo` | 200（fixture/labels 正常返回） |
| `POST /api/v1/demo/bootstrap`（需 `Idempotency-Key` header） | 201，返回 project/paper/extraction |
| `POST /projects/{pid}/experiments` | 201，`status=draft` |
| `GET /experiments/{eid}/draft` | 200，含 policy_hash 与完整 spec |
| `POST /projects/{pid}/spec-revisions` | 201，返回 spec_hash |
| `POST /spec-revisions/{rid}/confirm` | 201，返回 confirmation_id |
| `POST /projects/{pid}/runs`（mode=local-cpu） | 202 → demo worker 异步执行 → `status=completed, verdict=matched` |
| `POST /runs/{rid}/report-builds` + 轮询 report-status | `ready` |
| `GET /runs/{rid}/report` | 200，report.json/report.md 均可经 `/artifacts/{id}/download` 下载且 sha256 与 artifact_id 一致 |
| `POST /runs/{rid}/exports` | 整体 `partial`：`report_json`/`report_markdown` ready；`bundle_zip` failed=`EXPORT_INCOMPLETE` —— 这是设计行为（`export_jobs.py`：fixture run 无 evidence，拒绝打包声称完整复现的 ZIP）。前端 `GuidedPage` 按 item 渲染状态，partial 属预期呈现 |

历史结论修正：该轮只验证了 API 主线到 partial export，未通过 ZIP 完整交付验收。旧 Run 没有登记 evidence，拒绝 ZIP 是正确的保护行为，但不等于目标闭环已经完成。后端启动恢复也不等于浏览器交互恢复，必须单独验证。

## Guided Demo 后续验证进展（2026-09-28）

- 已实现真实 controlled-smoke 输出内容寻址、RunEvent evidence 登记、报告冻结时校验归属/哈希/字节数，以及 ZIP 包含登记证据。旧 Run 与已冻结报告不补写，空 evidence 的旧报告仍拒绝 ZIP。
- 集成测试覆盖新 Run → report ready → ZIP succeeded，逐项下载 MIME/字节数/SHA-256/Content-Disposition、ZIP checksums 和真实 evidence 内容一致。此项是测试客户端验证，不是浏览器下载验收。
- 已修复前端 DTO 校验：未注册导出项允许预定 filename；安全 code-only item.error 使用 code 作显示回退，不要求后端暴露异常文本。
- 刷新修复完成后的主代理独立回归：Python **408 passed, 1 warning**；前端 **57 passed**；TypeScript/Vite build、git diff --check 通过。compileall 在前一轮通过，本轮未重跑。warning 为 Starlette/httpx 弃用提示，未安装依赖。readRev 重启只读 effect、abort 与晚响应隔离已包含在前端测试中，但不等于浏览器刷新恢复验收完成。
- 浏览器阻断仍在：开发服务 5173 与 production preview 4173 均观察到“加载工作台布局…”。临时诊断日志证明 guided 动态 import 已 resolved；Console 未捕获错误，Network 诊断返回空集合，不能由此断言无请求或网络正常。临时入口诊断已撤回，未把删除 lazy 当作修复。
- API 8010 的 health 和经 Vite 的 `/api/v1/demo` 均实际返回 200。Preview 启动时未配置 API target，不能作为端到端 API 验证入口。
- 本轮临时诊断进一步观察到 GuidedPage 组件 render 两次，但页面仍是 Suspense fallback；注册的 1 秒 timer 在等待后未见对应 Console 记录。该现象不足以证明浏览器计时器或 React 调度故障，根因仍未确认。所有临时诊断已撤回，保留原 lazy/Suspense 架构。
- **尚未完成**：真实浏览器中新 Run/ZIP 下载闭环、刷新恢复与返回编辑全流程、显式失败重试/取消以及完整视觉/可访问性验收。显式刷新读取失败后的恢复代码及单元回归已完成，浏览器验收仍受加载态阻断。
- 安装打包仅验证配置回归测试；当前 venv 缺 setuptools/pip/build，真实 build/install 未验证。仍以 `PYTHONPATH=.:services/runner` 启动本地 API。

上述结果仅支持 local demo / controlled smoke / not full reproduction；不构成生产、安全隔离、真实论文性能或公网部署验收。

## 2026-09-28 晚间：浏览器主线与动画修复（部分验收）

本节为最新状态；前面加载态阻断描述保留作为历史，不代表当前一直无法打开页面。

- 浏览器实际完成 Paper → Extraction → Experiment → Run completed → Report ready → Export succeeded。项目 `a761cf2e-cb36-46de-8655-7bdea42e9204`，Run `run_b00f8d81-9d44-431c-9d19-3d8616bffbb2`，Export `exp_3363fd62-de79-4032-bac6-39a8984f5dc5`。
- RunEvent 包含 `run.evidence.registered`：188 bytes、application/json、SHA-256 `0829a0a325b0c824dc02c18e554db4745b8cf2924289edbadb00b3c19c5105c4`。报告指标来源为 controlled_fixture_evaluation，verdict 为 not_comparable，不是论文复现性能。
- 导出三项 ready：ZIP 2656 bytes、JSON 748 bytes、Markdown 352 bytes。curl 下载 ZIP 验证了 200、application/zip、字节数和 SHA-256 `3fdb169561e68b9c534ed4f39d9a1c68d0610f66097fdb2dbcfcd5f46116a174`；可列出 checksums、evidence、manifest、metrics、report.json、report.md。`.p2r-guided-verification/browser-bundle.zip` 实际由 curl 写入，文件名不能作为浏览器落盘证据。
- 浏览器重开页面后保留项目，进入 Export 可读回原 succeeded 导出；完整 reload GET-only 网络脚本两次遇到 MCP connection closed，未完成该项网络验收。
- 下载诊断观测到真实 blob URL 和 anchor.click，未出现页面 alert；分别试验延迟 revoke 和临时挂载 anchor 后仍未捕获 download 事件，临时修改均已恢复。这些结果不能确定下载故障根因，也不能证明浏览器已落盘。扩展连接中断后页面变成连接页，在该连接页执行的直接下载探针无效，不计入应用验证。
- 修复步骤动画：GuidedPage 的 section 增加 key={state.viewStep}，避免 React 复用同一节点导致切换步骤不重新触发进入动画。新增源码契约测试先失败再通过，保留焦点 effect 和 reduced-motion CSS。随后浏览器从 Paper 切至 Extraction 实测：animationstart 捕获 guided-enter，计算时长 0.24s，document.activeElement 为当前卡片；模拟 prefers-reduced-motion: reduce 后 animationName 为 none。此结果覆盖该次切换，不代表全部键盘/视觉验收完成。
- 最新前端回归：58 passed，TypeScript/Vite build 成功（119 modules），git diff --check 通过。最新后端全量回归：410 passed，1 条 Starlette/httpx 弃用警告；本轮新增报告 Worker 的 owner/fencing token 中间状态保护及两项回归测试。
- 产物范围说明：Playwright 扩展自动写入了 `.playwright-mcp/` 快照和 Console 记录，属于验证目录之外的工具产物，不能宣称所有文件均在 `.p2r-guided-verification/`。

返回编辑补充验收：浏览器点击“返回编辑”后回到 Experiment，Run/Report/Export 被禁用，预算确认被清除。重新勾选预算并确认后，POST spec-revisions 返回 201，新 revision 为 `spr_a3af509c-63f1-4b65-b54e-51e6cee53b3a`，其 confirm 返回 201；界面进入 Run 并显示“尚未启动”，未自动创建 Run。独立 HTTP 读回旧 Run `run_b00f8d81-9d44-431c-9d19-3d8616bffbb2` 仍为 completed，关联原 revision `spr_b065693a-c533-431d-81e7-7df4a64d6a13`，没有被新 revision 覆盖。

该时点仍待完成：浏览器下载落盘、刷新恢复 GET-only 网络验收、显式重试/失败/取消，以及完整视觉和键盘验收。单次步骤动画、焦点及 reduced-motion 已实测，不能代表全部交互通过。MiMo 子代理后端审查因上游连接失败未完成，不作为审查通过。主代理另已修复报告 Worker 中间状态写入缺少 owner/token guard，新增两项先失败后通过的测试，最新后端全量 410 passed。

## 2026-09-28 20:22：终态轮询与整页恢复

- 根因：pollRunAndReport 每次成功后无条件安排下一次 GET。新增终态和报告进行中测试，修复前 8 项失败；现在仅 Run 活跃或 report pending/generating 时继续轮询。GuidedPage effect 加入 report.build_id，新建/显式重试报告后可以重新启动只读轮询，保留 readRev 和 epoch。
- 新增真实 controller 的显式报告重试回归：failed → 一个 report-builds POST → 新 build_id/pending → GET 读回 ready → 停止，未重复 POST。该项为自动化测试，不冒充浏览器失败重试验收。
- 浏览器显式启动返回编辑后的 revision，生成 Run `run_5b29b542-f19f-4136-990b-0d0b37065612`，completed 且登记 188-byte JSON evidence。构建报告 POST 202 后成功读回 ready；网络记录 131–134 为 Run/events/report-status/report，随后两次查询记录不再增长，证明终态不持续轮询。
- 新 Export `exp_81139110-cb49-4cbc-a077-c44c2553c65b` succeeded，ZIP/JSON/Markdown 分别 ready（2656/748/352 bytes），尚未取得这批文件的浏览器落盘证据。
- 对当前应用页执行完整 page.reload 后，新增 API 请求 163–173 全为 GET；其中一次 demo GET 被取消，另一请求 200，其余恢复读取 200，无 POST。读取关联同一新 Run；页面恢复项目并解锁六步。当前实现刷新后显示 Paper，不恢复刷新前的 Export 视图，不能宣称原步骤原位恢复。
- 此前未启动 Run 的新 revision 上，“刷新（只读）”新增 111–112 也仅为 demo/extraction GET 200。
- 最新前端全量回归：67 passed；TypeScript/Vite build 成功（119 modules）；git diff --check 通过。本轮未修改后端，后端全量沿用此前 410 passed 的记录，未重新执行。
- Console 读取到两条扩展 LavaMoat 警告，以及 runtime.lastError/message channel closed、Receiving end does not exist 通信错误；没有据此证明下载问题根因，也不宣称 console clean。
- 刷新后点击 Export，读回同一 `exp_81139110-cb49-4cbc-a077-c44c2553c65b` succeeded 及三项 ready，未重新创建导出。随后点击 ZIP 下载仍未获得落盘证据；尝试为本次验证配置工作区下载目录时，扩展在 newCDPSession 拒绝 `Target.attachToBrowserTarget: Not allowed`，未成功设置下载目录，不绕过该限制。
- 仍待浏览器下载落盘、显式失败重试/取消、完整视觉与键盘验收。以上仅为 local demo / controlled smoke / not full reproduction。
