# Paper2Run 只保留自主模式：结果出口关卡、独立复核与 Windows 支持

日期：2026-11-05 · 状态：设计待批准 · 参考：PKU-YuanGroup/OpenAI4S（`docs/auto-mode.md`、`docs/windows-wsl.md`）

## 1. 目的与原因

当前产品有两条路径：固定六步的传统引导流程（`RealFlowPanel`、`/lab`、Mock、`p2r_runner`/`p2r_codegen`/`p2r_kernel`），以及上一轮加入的自主模式（Pi harness + Docker 隔离容器）。默认入口是传统流程，自主模式只是顶栏上的一个按钮，而且论文上传还要绕回传统向导。

新版本只保留自主模式。科研过程怎么推进由 Pi 自己决定，程序不规定步骤顺序，也不按阶段限制工具，以免束缚 Pi 的思考。程序只把守结果出口：什么样的证据才算候选结果、候选结果是否通过独立复核、最后落到哪种结局。完成判定参考 OpenAI4S 的 Auto Mode：候选 → 独立复核 → 已验证；复核不通过就由 Pi 自动修复，次数有上限；只有确定性规则判定"必须问人"时才打扰用户。

同时要支持 Windows。用户明确说过：容器只负责安全隔离和项目隔离，容器内部要给 Agent 足够的权限，不需要断网。

## 2. 目标、非目标、反目标

**目标**
- 产品只剩一个入口：自主实验。新建任务时，在自主界面里完成建项目、上传并解析论文、配置、授权和启动。
- Pi 自主推进科研。程序只在 `submit_candidate` 处检查证据是否齐全，然后冻结快照、评测、在新会话中复核；复核不通过时把问题交还 Pi 修复，轮次有上限。
- 结局只有固定的四种，每种只有一个进入条件，并写入数据库。
- 用户只在三种情况下需要介入：协议超出授权范围、预算耗尽、容器安全被破坏。
- macOS、Linux、Windows 使用同一种执行方式：Docker 中的 `paper2run-experiment:1` 容器。Windows 上通过 Docker Desktop（WSL2 后端）运行。

**非目标**
- 不规定科研步骤顺序，不按阶段设工具白名单，不做阶段推进或回退的关卡。
- 第一个协议不需要用户人工审批（不做 OpenAI4S 的 Plan 模式）。
- 不提供不依赖 Docker 的原生 Windows 进程沙箱。旧的 AppContainer/Job 方案权限太窄，访问不了 git、conda、编译器和系统目录，而且 Job 会随应用退出一起结束，后台训练活不下来，所以已经否决。
- 程序不评判科研质量，质量判断交给复核会话。
- 复核不强制使用不同的模型。
- 不在 Windows 上验证 GPU。

**反目标**（技术上能跑，但出现这些就算失败）
- 把"已验证"展示成"复现成功"。已验证只表示复核会话认可了这份候选结果；能否和论文比较，仍由评测结论（`not_comparable` 等）单独说明。
- 模型只靠写一段文字就能产出候选结果或改变结局。
- 在续写或提示词里替 Pi 规定下一步该做什么。
- 删除或改写数据库中已有的旧任务记录。
- 应用退出时把后台训练一起杀掉（现在已经保证不会，不能退化）。

## 3. 约束

- 分层（L0–L4）和三文件源码根同步这两个不变量测试必须一直通过。删除包时要同步更新 `LAYERS`、`pyproject.toml` 的两处列表以及 `scripts/launch.py` 的 `SOURCE_ROOTS`。
- 现有原则保持不变：证据类别由程序写入；模型的叙述不能升级结论；证据不足时返回 `not_comparable`；凭据不进入容器；事件表只能追加。
- 每个阶段在新分支 `feat/autonomous-only` 上独立提交，使用 conventional commits。
- 没有实机证据就不宣称支持。

## 4. 考虑过的做法

**关卡放在哪里**

| 做法 | 取舍 |
|---|---|
| **A. 关卡放在 Python 控制面（选定）** | 所有工具调用本来就经过 `worker.dispatch()`。规则只放在可信层，重启后可以恢复，能用 pytest 测试，Node 层基本不用改 |
| B. 关卡放在 Node harness | 状态分散在两处；Pi SDK 能否在会话中途换工具集还没验证 |
| C. 每个阶段一个新会话 | 上下文会断，token 消耗更高 |

**约束强度**

| 做法 | 取舍 |
|---|---|
| 8 个阶段硬状态机（初稿） | 步骤可预测，但会束缚 Pi 的思考，用户已否决 |
| **只在结果出口设关卡（选定）** | Pi 自主推进；可信性靠"证据齐全 + 冻结快照 + 独立复核 + 固定结局"来保证 |
| 只靠提示词软引导 | 没有可信的完成判定 |

**第一个协议是否需要人工审批**：选择不需要，理由如下：
1. OpenAI4S 的 Auto Mode 本身就不让人审批日常进展，人工审批计划属于它单独的 Plan 模式；
2. 用户启动任务时已经确认了目标、预算、执行位置和 `allowed_changes`，这就是事先同意；
3. 复核会话会对照冻结的协议检查实现与结果，可以兜底。

这是设计者的推荐，用户采纳了。

## 5. 已确定的设计

### 5.1 现有结构（据此改造）

- 工具调用链路：Pi 模型 → `apps/harness/bridge.mjs:312` 发出 `tool_request` → Python `worker.py:dispatch()` 执行 → 返回 `tool_result`。工具清单只在会话开始时下发一次（`worker.py:285`）。
- `research_delegate`（专家建议）直接在 Node 中执行，输出一律标记为 `unverified_advisory`。
- 任务表是 `autonomous_tasks`，`state` 字段表示生命周期（queued/running/paused/stopping/stopped/completed/incomplete…）；事件写在 `autonomous_events`，只能追加。
- 目前会话一旦以 `result` 结束，就会自动生成报告，并把任务置为 `completed`/`incomplete`（`worker.py:238–248`）。

### 5.2 新增状态

通过 alembic 迁移 0022 在 `autonomous_tasks` 新增两列：
- `outcome`：结局（见 5.6），到终态之前为空；
- `harness_json`：控制面的 harness 状态。新建任务写入 `{"version": 2, "repair_round": 0, "no_progress": 0, "fingerprint": null, "candidate": null, "reviews": []}`，之后还会出现 `outcome_snapshot_sha256` 和 `delivered_user_messages`。`harness_json` 为空表示这是 harness 改造之前创建的旧任务。

`repair_round`、当前候选（快照哈希、复核状态、尝试次数）和历次复核记录都放在 `harness_json` 里，不单独建列。`state` 保持原有的生命周期含义，暂停、停止、恢复、预算这些机制都不变。

### 5.3 工具

- 现有工具全部保留，Pi 任何时候都能调用：read, write, execute, background, status, logs, stop, propose_protocol, register_artifact, research_delegate。
- `register_artifact` 的 `kind` 新增 `analysis`（论文分析），可以选择使用，不作要求。
- 新增 `note_progress(phase, summary)`：Pi 自报当前在做什么，只用于展示，不设任何关卡。`phase` 是自由文本，长度有上限。工作台时间线优先展示这些自报内容；没有的话，就退回现有的按工具事件归组（`stages.ts`）。
- 新增 `submit_candidate(summary)`：见 5.4。
- 原有的 `evaluate` / `report` 工具不再直接开放给 Pi。评测在 `submit_candidate` 里由程序运行；报告在任务进入终态时由程序生成，Pi 在 `submit_candidate` 里附带的叙述会作为"模型叙述"写入报告。这样就保留了"叙述不能确立成功"这一规则。
- 系统提示词改为说明：目标、可用工具、`submit_candidate` 的证据要求、复核与修复机制、四种结局的含义。提示词不规定步骤顺序。

### 5.4 提交候选（唯一的证据关卡）

`submit_candidate` 依次检查以下条件，任何一项不满足都拒绝，并列出缺少的具体项：
1. 已有冻结的协议版本；
2. 当前轮次中已登记了 `code`、`environment`、`data`、`result` 四类产物；
3. 当前轮次的某个 `code` 产物就是协议入口（按产物名或源路径匹配），缺少时列为 `entrypoint code artifact`；
4. 协议 `expected_artifacts` 中的每个名称都已在当前轮次登记，每缺一个列一项 `expected artifact <name>`；
5. 至少有一次入口运行以退出码 0 结束，它的 argv 与协议入口一致（沿用现有"直接用解释器启动入口"的规则），并且该作业的**启动序号**晚于当前轮次最后一次登记 `code` 的序号。按启动序号而不是结束时间或观察时间判断，所以重新查询一个旧作业不会让它变成"新鲜"的运行。

检查通过后冻结快照：协议版本、轮次、正式运行作业的 id/退出码/启动序号、四类产物的名称/类别/sha256。对这些内容做规范化序列化后计算哈希。快照引用的产物按哈希校验后复制到 `task_root/candidates/<sha>/artifacts/`，正式运行作业的日志分页取回，写入 `task_root/candidates/<sha>/logs/`；冻结时的协议（`{protocol, sha256}`）写入 `task_root/candidates/<sha>/protocol.json`，并保存在候选记录里。每份冻结候选的清单都保留在 `harness_json.candidates[<sha>]`，`harness_json.candidate` 仍是当前候选。最后写入 `candidate_frozen` 事件，候选的复核状态为 `pending`。

候选处于 `pending` 期间，`register_artifact`、`submit_candidate` 和 `propose_protocol` 一律被拒绝（`CANDIDATE_UNDER_REVIEW`），证据在复核结束前不会再变。`submit_candidate` 的返回值告诉 Pi 结束本轮。

### 5.5 评测、复核与自动修复（在提交候选的那一轮结束后由程序执行）

复核不放在 `submit_candidate` 调用内同步完成。harness 对单次工具调用有 300 秒的超时上限（`apps/harness/bridge.mjs:305`），一次复核会超过它。所以 `submit_candidate` 只负责冻结并立即返回，Pi 结束当前轮次后，由 `driver.after_turn` 接手：

1. 运行现有的可信评测（对冻结副本、按该候选冻结时的协议重算），可比性结论原样保存。
2. 新开一个 harness 会话作为复核者：使用新的会话目录，模型沿用主 Agent 的配置（允许同一个模型）。复核者只有 `read`、`logs`、`status` 和 `submit_review` 这几个工具，这些工具指向 `FrozenTarget`，只能访问冻结副本和冻结日志，**不连接实验容器**。复核者的输入包括：论文上下文（按论文字段白名单取，不含用户消息和取消意图等控制状态）、冻结协议、快照清单、评测结果。复核期间，用户的引导消息不会送到复核者，只会留在库里，等主会话取用。
3. `submit_review` 的结构：`verdict: pass | issues`；`findings[]`，每条包含描述和引用的证据（产物名、源路径或日志摘录）。`issues` 至少要带一条带证据的 finding。复核结果绑定到快照哈希，写入 `review_recorded` 事件。
4. 根据结果分支：
   - `pass` → 结局 `verified`，程序生成报告。
   - `issues`，且修复轮次没用完 → `repair_round + 1`，findings 原样写进下一条续写消息送给 Pi。怎么修、修哪里、要不要改协议，都由 Pi 决定。修好后再次调用 `submit_candidate`。
   - `issues`，且轮次已用完 → 结局 `completed_with_issues`。
   - 复核会话失败、超时或没有产出合法的 `submit_review`：重试 1 次（共 2 次尝试），仍失败就是 `review_unavailable`，原因记为 `review_failed`。
5. 修复轮次上限是 `limits.max_repair_rounds`，默认 3，取值 0 到 10，用户可以调小。复核消耗的 token 和时间计入同一个任务预算。
6. 如果预算在复核过程中耗尽：此前已有复核记录的，结局为 `completed_with_issues`，原因记为 `budget_exhausted`，绑定最近一次被复核的快照；没有的话，结局为 `review_unavailable`，原因同样是 `budget_exhausted`。
7. 恢复：复核过程中如果暂停或应用退出，且复核者还没有提交合法的 `submit_review`，候选仍是 `pending`，恢复后针对同一份快照重新复核。复核结果只对它所复核的那份快照有效，快照对不上就算一次失败的尝试。
8. 已完成的复核一定保留：只要复核会话为当前快照返回了合法结论，不论任务此时是否还在运行（复核会话最后一次用量触发预算停止、提交后暂停或应用退出），都写入 `review_recorded`、复核记录和候选的复核状态。之后：任务仍在运行，照第 4 步继续；不在运行且结论为 `pass`，结局为 `verified`，生成报告，生命周期状态保持停止流程给出的状态，不再恢复主会话；不在运行且结论为 `issues`，修复轮次已用完则结局为 `completed_with_issues`，预算停止则按第 6 步得出绑定这份快照的 `completed_with_issues`，暂停则在恢复时直接进入修复轮次，不再复核。续写消息中的剩余预算在预算耗尽时写作 `exhausted`，不报错。harness 被中止时会以错误消息结束会话（`aborted`、`budget_exceeded`、`wall_time_exceeded`、`bridge_disconnected`、`stdin_eof`、`tool_timeout`）；如果这类中断发生在 `submit_review` 被接受之后，结论照常返回，不算复核失败。提交之前的中断，以及其他错误码（例如 `provider_error`），仍按复核失败处理。

### 5.6 结局

| outcome | 唯一进入条件 |
|---|---|
| `verified` | 最新候选有一条绑定其快照哈希的 `pass` 复核记录 |
| `completed_with_issues` | 最新候选的复核结果是 `issues`，并且修复轮次已用完；或者预算耗尽，但已经存在复核记录 |
| `review_unavailable` | 复核会话两次尝试都失败；或者预算耗尽时候选还在等待复核，且此前没有复核记录 |
| `blocked_by_safety` | 容器隔离完整性校验失败，错误码为 `isolation_changed`、`network_isolation_failed` 或 `host_addresses_changed`。第一次发生就终止，不能恢复。此前这类错误只会让任务暂停 |

每个结局都写入 `outcome_snapshot_sha256`，记录是哪份快照决定了它（没有候选时为空）。报告中的候选、产物、评测、完成检查和叙述都取自这份决定结局的快照，评测按它自己冻结的协议计算；没有结局时取当前候选。评测结论 `verdict` 在所有结局下都是 `not_comparable`，结局不改变可比性。

状态和结局的对应：
- `verified`、`completed_with_issues`（复核耗尽轮次）、`review_unavailable`（复核失败）：`state` 为 `completed`。
- 预算耗尽后得出的结局：`state` 保持 `stopped`，`outcome` 有值。预算停止仍然是生命周期上的停止，结局只说明复核层面的结果。
- 任务已在暂停或停止时才记录下的复核结局（5.5 第 8 步）：`state` 保持暂停或停止流程给出的状态，`outcome` 有值。
- `blocked_by_safety`：`state` 最终为 `stopped`，`outcome` 为 `blocked_by_safety`，所有作业被取消。它不可撤销。若有候选正在等待复核，该候选标为复核不可用，不再复核。
- 任何有 `outcome` 的任务再调用恢复，返回 `TASK_FINISHED`。

`SECRET_IN_ARTIFACT`、路径逃逸等仍然按单次调用拒绝，不会让任务终止。没有产出候选就停止、失败或耗尽预算的任务，保持现有的生命周期状态，`outcome` 为空。报告由程序在任务到达终态时生成，`evaluate` 和 `report` 不再是工具。界面要分开展示两件事：结局（复核层面）和可比性（评测层面）。`verified` 的文案写"已复核"，不写"复现成功"。

### 5.7 会话中途结束

如果主会话的一轮结束，但任务还没有到达终态，也没有待复核的候选，Python 会以续写消息继续同一个会话。续写消息**只陈述事实**：
- 当前修复轮次和剩余修复轮次；
- 当前轮次已登记的产物（名称、类别）；
- 各作业的状态、退出码和命令；
- `submit_candidate` 还缺哪几项，没有缺项时写"none"；
- 剩余预算；
- 最近一次复核的 findings，原文。

自上一轮以来用户发来的消息也会附在续写后面，逐条原文引用（过长的消息只能引用预览，并标注 `[truncated]`）。续写消息不建议下一步做什么。

连续 3 次续写都没有进展，任务就以 `incomplete` 结束，`outcome` 为空。进展按三个量的指纹判断：已登记产物数、已结束作业数、当前候选的快照哈希。三者都没变就算一次无进展，有变化则计数清零。续写同样消耗 token 预算。

### 5.8 证据按轮次存储

现有规则是"同一协议版本内产物名称不可变"（`evidence.py:46`，存储在 `artifacts/<protocol_sha>/<name>`），改为"同一协议版本、同一轮次内名称不可变"。存储路径改为 `artifacts/<protocol_sha>/r<round>/<name>`，256 个的上限按轮次计算。`evaluate_artifacts`、`verified_files` 和导出只读取当前候选所属的轮次。旧数据没有轮次目录，仍可只读。

### 5.9 迁移前的自主任务

`harness_json` 为空的旧自主任务不能恢复，控制接口返回 `LEGACY_TASK`："该任务创建于新版 harness 之前，请新建任务"，但仍可只读导出。已经有结局的任务恢复时返回 `TASK_FINISHED`。

### 5.10 删除传统模式

> 已实施（2026-11-05，`feat/v3-integration` 分支）：后端、脚本与文档中的旧模式已删除，前端部分此前已合并（PR #1）。

**前端**：删除 `features/guided`、`features/lab`、`features/papers`、`features/runs`、`features/reports`、`MockWorkspace.tsx`、`api-client/guided*`、`api-client/phase01*`、`api-client/mock`，以及 `app.tsx` 和 `routing.ts` 中的旧路由；删除对应的旧测试。`Dashboard`/`TaskDetail`/`Sidebar` 改为读取自主任务 API。新建任务流程放进自主界面：建项目 → 上传论文 → 解析（复用 docparse 组件）→ 配置与授权 → 启动。工作台展示自报进度、候选轮次、复核 findings 和结局。

**后端**：
- 整包删除 `p2r_runner`、`p2r_codegen`、`p2r_kernel`，同步更新 `LAYERS` 和三文件源码根。
- `p2r_sandbox` 只保留 `autonomous/` 子包和它依赖的文件；`sandbox.py`、`_winappc.py`、`_winjob.py`、`_landlock_launcher.py` 等旧进程沙箱模块删除。包仍然保持 standalone。
- `p2r_orchestration` 中只服务旧流程的模块删除，包括 `run_worker`、`report_builder`、`repair_repository`、`codegen_service`、旧 `service.py`、`environment_checker`、`extraction_service`、`lenient_extraction` 等。
- `p2r_api` 中只服务旧流程的路由删除，包括 `demo`、`codegen`、`runs`、`reports`、`sessions`、`experiments` 等，以及 `demo.py`、`demo_receipts.py` 和 `create_demo_app`。启动器改为使用 `create_app()`，并默认启用自主路由。
- **保留**：项目、论文上传与内容、docparse、认证、自主路由、设置相关接口。
- `paper_context()` 只读取经过校验的解析文本，不再读取 `ExtractionRecord`。

**数据库**：不删表，不删行，不做破坏性迁移。

**文档**：更新 README、AGENTS.md、`docs/architecture.md`；在 12/13 号文档开头注明"旧模式保留"这一条已被本设计取代。

### 5.11 Windows

执行环境：Docker Desktop（WSL2 后端）运行同一个 Linux 容器。容器内的 `iptables`、`fcntl` 等跑在 Linux 中，不需要改。

宿主侧需要移植的地方：
- `research_capabilities.py:97` 的技能注册表锁和 `autonomous/service.py:231` 的任务租约锁：改用跨平台锁助手，POSIX 上用 `fcntl.flock`，Windows 上用 `msvcrt.locking` 或 `LockFileEx`。
- `p2r_evaluation/autonomous.py:88` 和 `research_capabilities.py` 中依赖 `O_NOFOLLOW`/`dir_fd` 的防符号链接读取：Windows 上要提供等价的安全语义，拒绝链接和重解析点，并确认最终路径仍在根目录内，**不能直接放宽**。
- `p2r_sandbox/autonomous/_interfaces.py` 的宿主网卡枚举：Windows 上用 `GetAdaptersAddresses`（ctypes）实现。
- `apps/desktop/main.cjs:182` 去掉对 Windows 的拒绝；启动时检查 `docker info` 的 OSType 是否为 linux，以及 Docker 是否可用，不满足时给出中文指引。
- `scripts/launch.py` 和 `启动Paper2Run.bat` 改为在 Windows 上走自主模式。
- Windows 宿主路径挂载进容器、SSH 目标使用 Windows OpenSSH：由实现者验证，不能工作的部分如实列为限制。

### 5.12 实施顺序

1. 后端：结果出口关卡、复核与修复、结局、续写、轮次存储（用 pytest 覆盖）
2. 前端：只保留自主模式，新建任务流程，工作台展示
3. 删除传统模式的前后端代码和文档
4. Windows 移植

## 6. 交给实现者决定的事

- 新增字段放在列中还是 JSON 中；迁移的具体写法。
- 续写消息、拒绝提示和复核者系统提示词的措辞（续写消息必须只陈述事实）。
- 复核会话复用 `AutonomousBridge` 的方式，以及同步复核期间 harness 工具调用的超时设置。
- 跨平台锁助手放在哪个模块（要符合分层规则）。
- 旧模块删除清单的最终确认：先用 grep 确认没有保留代码引用，再删除；如果发现自主模式确实依赖某个模块，就保留并在提交说明里写明。
- 新建任务界面的布局细节。

## 7. 验收

- pytest：`submit_candidate` 每项检查的通过与拒绝；pass/issues/轮次耗尽/复核失败/复核中预算耗尽这几条分支；复核者只能访问冻结副本；复核恢复的幂等性；`blocked_by_safety`；续写无进展时终止；轮次存储与不可变性；旧任务拒绝恢复。复核模型和主模型都用测试替身。
- 两个不变量测试通过；前端 `typecheck` 与 `test` 通过。
- 真实端到端：至少一个任务用真实模型和 Docker 跑到四种结局之一，并且最好经历过一次修复。记录在验收文档中。
- Windows：有实机时完成一次端到端运行；没有实机时如实标注"未实机验证"，并给出单元测试覆盖范围。

## 8. 实现偏差

后端已按本设计实现（提交到 `d645fe2`）。下面几处与原文不同，以代码为准：

1. 复核在提交候选的那一轮结束后执行，不在 `submit_candidate` 内同步执行。原因是单次工具调用 300 秒的超时（5.5）。
2. 新增状态放在 `outcome` 列和 `harness_json` 中，不单独建 `repair_round` 与快照哈希列（5.2）。
3. 运行"新鲜度"按作业启动序号比较，不按结束时间（5.4）。
4. 候选待复核期间，`register_artifact`、`submit_candidate`、`propose_protocol` 被拒绝（5.4）。
5. 预算耗尽得出结局时，`state` 保持 `stopped`，不改成 `completed`（5.6）。
6. 每个结局记录 `outcome_snapshot_sha256`；评测 `verdict` 始终为 `not_comparable`（5.6）。
7. 续写消息还包含自上一轮以来收到的用户消息，并在同一条续写里带上最近的 findings（5.7）。
8. 旧任务以 `harness_json` 为空识别，而不是 `outcome`/`repair_round` 为空（5.9）。
9. 复核进行时，暂停、停止和应用退出会中止复核会话，用户消息不转发给复核者（5.5）。
10. 提交关卡与报告的完整性检查使用同一套规则：入口代码产物和协议 `expected_artifacts` 都是提交条件，因此 `verified` 的报告不会是 `incomplete`（5.4）。
11. 预算耗尽时结局绑定较早一份已复核快照的情况下，报告按那份快照及其冻结协议生成，而不是按更新的待复核候选或任务当前协议（5.4、5.6）。
12. 已提交的复核结论在任务停止、暂停或预算耗尽时仍会写入，不会被丢弃后重新付费复核；`facts_message` 在预算耗尽时不抛错（5.5 第 8 步）。
13. 协议变更的审批有显式决策接口 `POST /api/v1/autonomous/tasks/{task_id}/decisions/{decision_id}`，请求体为 `{action: "approve" | "reject", note?}`（`note` 最长 4000 字符），返回公开任务（5.3）。`decision_id` 必须等于当前 `pending_decision.id`，否则返回 409 `DECISION_MISMATCH`；任务必须处于 `paused`，否则返回 409 `INVALID_STATE`；旧任务、已有结局的任务以及恢复的其余前置条件与恢复共用同一段代码。`approve` 应用提议的协议并写入 `protocol_approved`。`reject` 保留当前协议，写入 `protocol_rejected`（`decision_id`、`proposed_sha256`、`changed`、`note`），并按用户消息的同一条路径把拒绝告知 Pi，然后同样恢复任务。原来的 `control(resume, "approve:<id>")` 仍然有效；存在待审批决策时不带消息的恢复返回 409 `DECISION_PENDING`。

5.10 到 5.12（前端、删除传统模式、Windows）仍是后续工作，本次未改动。
