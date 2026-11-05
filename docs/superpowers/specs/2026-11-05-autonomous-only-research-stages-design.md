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

通过新的 alembic 迁移新增：
- `outcome`：结局（见 5.6），到终态之前为空；
- `repair_round`：当前证据轮次，从 0 开始；
- 当前候选的快照哈希，以及复核状态（无 / 进行中 / 已记录）。

`state` 保持原有的生命周期含义，暂停、停止、恢复、预算这些机制都不变。

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
3. 至少有一个后台作业以退出码 0 结束，它的 argv 与协议入口一致（沿用现有"直接用解释器启动入口"的规则），并且在当前轮次最后一次登记 `code` 之后才结束。

检查通过后冻结快照：协议版本、四类产物的名称/路径/sha256、正式运行作业的 id 与退出码。对这些内容做规范化序列化后计算哈希。快照引用的产物文件和正式运行作业的日志会一并复制到宿主侧的证据存储。最后写入 `candidate_frozen` 事件。

### 5.5 评测、复核与自动修复（在 `submit_candidate` 调用内同步完成）

1. 运行现有的可信评测，可比性结论原样保存。
2. 新开一个 harness 会话作为复核者：使用新的会话文件，模型沿用主 Agent 的配置。复核者只有 `read`、`logs`、`status` 和 `submit_review` 这几个工具，而且这些工具指向一个只读目标，只能访问冻结副本和作业记录，**不连接实验容器**。复核者的输入包括：论文上下文、冻结协议、快照清单、评测结果。
3. `submit_review` 的结构：`verdict: pass | issues`；`findings[]`，每条包含描述和引用的证据（产物名或作业 id）。`issues` 至少要带一条 finding。复核结果绑定到快照哈希，写入 `review_recorded` 事件。
4. 根据结果分支：
   - `pass` → 进入终态 `verified`，程序生成报告。
   - `issues`，且修复轮次没用完 → `repair_round + 1`，把 findings 原样返回给 Pi。怎么修、修哪里、要不要改协议，都由 Pi 决定。修好后再次调用 `submit_candidate`。
   - `issues`，且轮次已用完 → 结局 `completed_with_issues`。
   - 复核会话超时、模型失败，或输出无法解析：重试 1 次，仍失败就是 `review_unavailable`。
5. 修复轮次上限默认 3 次，写在项目配置的预算里，用户可以调小。复核消耗的 token 和时间计入同一个任务预算。
6. 如果预算在复核过程中耗尽：之前已有复核过的候选，结局为 `completed_with_issues`；没有的话，结局为 `review_unavailable`，原因记为 `budget_exhausted`。
7. 恢复：复核过程中如果暂停或应用退出，恢复时只要还没有已记录的复核结果，就针对同一份快照重新复核。因为复核结果绑定快照，重复复核是幂等的。

### 5.6 结局

| outcome | 唯一进入条件 |
|---|---|
| `verified` | 最新候选有一条绑定其快照哈希的 `pass` 复核记录 |
| `completed_with_issues` | 最新候选的复核结果是 `issues`，并且修复轮次已用完；或者预算耗尽，但已经存在复核过的候选 |
| `review_unavailable` | 复核会话重试后仍失败；或者预算在复核过程中耗尽，且此前没有复核过的候选 |
| `blocked_by_safety` | 容器隔离完整性校验失败（iptables 规则被篡改，或宿主/元数据地址可以访问）。第一次发生就终止，不能恢复。现在这类错误只会让任务暂停，这里改为终止 |

`SECRET_IN_ARTIFACT`、路径逃逸等仍然按单次调用拒绝，不会让任务终止。没有产出候选就停止、失败或耗尽预算的任务，保持现有的生命周期状态，`outcome` 为空。界面要分开展示两件事：结局（复核层面）和可比性（评测层面）。`verified` 的文案写"已复核"，不写"复现成功"。

### 5.7 会话中途结束

如果主会话以 `result` 结束，但任务还没有到达终态，Python 会以续写消息自动继续同一个会话。续写消息**只陈述事实**：已登记的产物、作业状态、`submit_candidate` 还缺哪几项、剩余预算和剩余修复轮次。它不建议下一步做什么。如果连续 3 次续写都没有进展（没有新的已登记产物、没有作业结束、没有新提交的候选），任务就以 `incomplete` 结束，`outcome` 为空。续写同样消耗 token 预算。

### 5.8 证据按轮次存储

现有规则是"同一协议版本内产物名称不可变"（`evidence.py:46`，存储在 `artifacts/<protocol_sha>/<name>`），改为"同一协议版本、同一轮次内名称不可变"。存储路径改为 `artifacts/<protocol_sha>/r<round>/<name>`，256 个的上限按轮次计算。`evaluate_artifacts`、`verified_files` 和导出只读取当前候选所属的轮次。旧数据没有轮次目录，仍可只读。

### 5.9 迁移前的自主任务

`outcome`/`repair_round` 为空的旧自主任务不能恢复，控制接口返回"该任务创建于新版 harness 之前，请新建任务"，但仍可只读导出。

### 5.10 删除传统模式

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
