# Paper2Run 只保留自主模式：标准科研阶段、独立复核与 Windows 支持

日期：2026-11-05 · 状态：设计待批准 · 参考：PKU-YuanGroup/OpenAI4S（`docs/auto-mode.md`、`docs/windows-wsl.md`）

## 1. 目的与原因

当前产品有两条路径：固定六步的传统引导流程（`RealFlowPanel`、`/lab`、Mock、`p2r_runner`/`p2r_codegen`/`p2r_kernel`），以及上一轮加入的自主模式（Pi harness + Docker 隔离容器）。传统流程是默认入口，自主模式只是顶栏的一个按钮，而且论文上传还要绕回传统向导。

新版本只保留自主模式：由 AI 驱动 harness 做科研，但科研步骤是标准化的，由程序强制执行，而不是只靠提示词引导。AI 决定每一步怎么做，程序决定每一步做完没有、能不能进入下一步、最终结果属于哪种结局。完成判定参考 OpenAI4S 的模式：候选结果 → 独立复核 → 已验证；复核不通过就自动修复，次数有上限。

同时要支持 Windows。用户明确说过：容器只负责安全隔离和项目隔离，容器内部要给 Agent 足够的权限，不需要断网。

## 2. 目标、非目标、反目标

**目标**
- 产品只剩一个入口：自主实验。新建任务时，在自主界面里完成建项目、上传并解析论文、配置、授权和启动。
- harness 强制执行 8 个阶段。每个阶段只允许调用该阶段的工具；AI 申请进入下一阶段时，由程序对照数据库里的事实做检查。
- 提交候选结果时冻结证据快照，之后进行程序评测和复核。复核在一个新开的会话中进行，可以使用同一个模型。复核不通过就自动退回修复，轮次和预算都有上限。
- 结局只有固定的几种，每种只有一个进入条件，并且写入数据库。
- macOS、Linux 和 Windows 使用同一种执行方式：Docker 中的 `paper2run-experiment:1` 容器。Windows 上通过 Docker Desktop（WSL2 后端）运行。

**非目标**
- 不提供不依赖 Docker 的原生 Windows 进程沙箱。旧的 AppContainer/Job 方案权限太窄，访问不了 git、conda、编译器和系统目录，而且 Job 会随应用退出一起结束，后台训练活不下来，所以已经否决。
- 首版不按阶段向模型隐藏工具。模型能看到全部工具，被拦的调用会收到说明。（这是做法 B，等验证 Pi SDK 支持会话中途换工具集之后再考虑。）
- 程序只检查交付物的存在性、结构和新鲜度，不评判科研质量。质量判断交给复核会话。
- 不强制复核使用不同的模型。
- 不在 Windows 上验证 GPU。

**反目标**（技术上能跑，但出现这些就算失败）
- 把"已验证"展示成"复现成功"。已验证只表示复核会话认可了这份候选结果，可比性仍然由评测结论（`not_comparable` 等）单独说明。
- AI 只靠写一段文字就能通过阶段检查。
- 删除或改写数据库中已有的旧任务记录。
- 应用退出时把后台训练一起杀掉（现在已经保证不会，不能退化）。
- 被拦截或回退时没有留下事件记录。

## 3. 约束

- 分层（L0–L4）和三文件源码根同步这两个不变量测试必须一直通过。删除包时要同步更新 `LAYERS`、`pyproject.toml` 的两处列表以及 `scripts/launch.py` 的 `SOURCE_ROOTS`。
- 现有原则保持不变：证据类别由程序写入；模型的叙述不能升级结论；证据不足时返回 `not_comparable`；凭据不进入容器；事件表只能追加。
- 每个阶段在新分支上独立提交，使用 conventional commits。
- 没有实机证据就不宣称支持。如果没有 Windows 实机验收，就在文档里标明"Windows 未实机验证"。

## 4. 考虑过的做法

| 做法 | 内容 | 取舍 |
|---|---|---|
| **A. 状态机放在 Python 控制面（选定）** | 当前阶段写入数据库；`worker.dispatch()` 先检查工具是否允许调用；新增 `advance_stage` / `request_rollback`，由 Python 检查是否放行；复核由 Python 另开 harness 会话 | 规则只放在可信层，重启后可以恢复，能用 pytest 测试，Node 层基本不用改。代价是模型可能调用当前阶段不允许的工具，白费一轮 |
| B. 状态机放在 Node harness | `bridge.mjs` 按阶段替换工具集 | 状态分散在两处，交付物检查仍然要靠 Python；Pi SDK 能否在会话中途换工具集还没验证 |
| C. 每个阶段一个新会话 | Python 按阶段启动新会话，附交接摘要 | 工具可见性最严格，但上下文会断，修复循环要来回切会话，token 消耗也更高 |

用户选择了 A，理由是它和"程序决定，模型不分配证据类别"一致。B 以后可以作为优化叠加上去。

## 5. 已确定的设计

### 5.1 现有结构（据此改造）

- 工具调用链路：Pi 模型 → `apps/harness/bridge.mjs:312` 发出 `tool_request` → Python `worker.py:dispatch()` 执行 → 返回 `tool_result`。工具清单只在会话开始时下发一次（`worker.py:285`）。
- `research_delegate`（专家建议）直接在 Node 中执行，不经过 Python，它的输出一律标记为 `unverified_advisory`。
- 任务表是 `autonomous_tasks`，`state` 字段表示生命周期（queued/running/paused/stopping/stopped/completed/incomplete…）；事件写在 `autonomous_events`，只能追加。

### 5.2 阶段与生命周期分开

新增两列（通过新的 alembic 迁移）：
- `stage`：当前科研阶段（见 5.3）。
- `outcome`：结局（见 5.6），到终态之前为空。
- 另外记录 `repair_round`（已用的修复轮次）和每个阶段最近一次进入时对应的事件序号。可以放在新列里，也可以放在 JSON 中，由实现者决定。

`state` 保持原来的生命周期含义：暂停、停止、恢复、预算耗尽等机制都不变，阶段和生命周期互不干扰。旧任务行的 `stage`/`outcome` 为空，界面不再展示这些旧任务。

### 5.3 八个阶段

| # | stage | 允许的工具（`research_delegate` 在所有阶段都可用） | 进入下一阶段的检查（由程序执行） |
|---|---|---|---|
| 1 | `paper_analysis` 读论文 | read, write, execute, register_artifact, advance_stage | 本阶段登记过一个 `analysis` 类产物，它是可以解析的 JSON，并且至少包含 `method`、`datasets`、`metrics`、`reference_results`、`missing` 这几个键（值可以为空或写"未知"） |
| 2 | `protocol` 定方案 | read, propose_protocol, advance_stage, request_rollback | 已有冻结的协议版本，并且**用户已批准**。首个协议一定要人工审批；之后协议在授权范围内的变更沿用现有规则，不暂停 |
| 3 | `environment` 搭环境 | read, write, execute, background, status, logs, stop, register_artifact, advance_stage, request_rollback | 本阶段登记过 `environment` 类产物 |
| 4 | `implement` 实现+冒烟 | 同 3 | 本阶段登记过 `code` 类产物；并且在**最近一次 code 登记之后**，至少有一次 execute 或后台作业以退出码 0 结束 |
| 5 | `formal_run` 正式运行 | 同 3 | 一个后台作业以退出码 0 结束，它的 argv 与协议入口一致（沿用现有"直接用解释器启动入口"的规则）；之后登记过 `result` 类产物 |
| 6 | `candidate` 提交候选 | read, register_artifact, submit_candidate, request_rollback | `submit_candidate` 本身就是检查：code/environment/data/result 四类产物都要在当前修复轮次中登记过，否则拒绝，并列出缺少的类别 |
| 7 | `review` 评测与复核 | 主 Agent 在此阶段不调用工具（由 `submit_candidate` 同步完成，见 5.5） | 见 5.5 |
| 8 | `report` 报告 | read, report | `report` 成功后写入终态 |

**新鲜度规则**：检查时只认在该阶段最近一次进入之后登记或运行的事实，判断依据是事件序号。所以回退之后，必须重新产出交付物。

**拦截格式**：如果调用了当前阶段不允许的工具，`dispatch()` 不执行，返回结构化错误，内容包括当前阶段、本阶段允许的工具、尚缺的交付物，同时写入一条 `tool_blocked_by_stage` 事件。这条错误不消耗修复轮次。

**`advance_stage(reason)`**：检查通过时写入 `stage_advanced` 事件，并返回新阶段的说明（目标、允许的工具、交付物要求）；不通过时返回缺少的具体项，写入 `stage_advance_refused`。系统提示词改成描述这 8 个阶段和这些工具。

**`request_rollback(target_stage, reason)`**：只能退回到更早的阶段。退回到 `protocol` 时，沿用现有协议变更规则（超出授权就暂停，等用户批准）。写入 `stage_rolled_back` 事件。主动回退和复核导致的回退都计入修复轮次。

`register_artifact` 的 `kind` 枚举新增 `analysis`。

### 5.4 证据快照

`submit_candidate` 冻结一份快照：协议版本、当前轮次中四类产物的名称/路径/sha256，以及正式运行作业的 id 和退出码。对快照内容做规范化序列化后计算哈希，写入 `candidate_frozen` 事件。之后对这些产物的任何修改都必须进入新一轮，生成新的候选。

### 5.5 评测与复核（在 `submit_candidate` 调用内同步完成）

1. 进入 `review` 阶段，运行现有的可信评测（`evaluate`）。得到的可比性结论（`not_comparable` 等）原样保存。
2. Python 另开一个 harness 会话作为复核者：使用新的会话文件，模型沿用主 Agent 的配置。它只有 `read`、`logs`、`status` 和 `submit_review` 这几个工具，不能执行命令、不能写文件、不能委派。输入包括论文分析产物、已批准的协议、证据快照清单和评测结果。复核者使用单独的系统提示词，要求它对照协议检查实现、数据、运行和结果是否一致。
3. `submit_review` 的结构：`verdict: pass | issues`；`findings[]`，每条包含描述、引用的证据（产物名或作业 id）、`rework_stage: implement | formal_run | environment`。`issues` 至少要带一条 finding。复核结果绑定到快照哈希，写入 `review_recorded` 事件。
4. 根据结果分支：
   - `pass` → 进入 `report` 阶段。`submit_candidate` 向主 Agent 返回"复核通过"。
   - `issues`，且修复轮次没用完 → `repair_round + 1`，回退到所有 finding 中最早的 `rework_stage`，把 findings 返回给主 Agent。
   - `issues`，且轮次已用完 → 进入 `report` 阶段，结局预定为 `completed_with_issues`。
   - 复核会话超时、模型失败，或输出无法解析：允许重试 1 次，仍失败就进入 `report` 阶段，结局预定为 `review_unavailable`。
5. 修复轮次上限默认 3 次，写在项目配置的预算里，用户可以调小。现有的 token 预算继续生效，哪个先用完就按哪个结束。
6. 恢复：复核过程中如果发生暂停、停止或应用退出，复核状态以"候选快照哈希 + 进行中"的形式持久化。恢复时，如果没有已记录的复核结果，就针对同一份快照重新复核。由于复核结果绑定快照，重复复核是幂等的。

### 5.6 结局

| outcome | 唯一进入条件 |
|---|---|
| `verified` | 最新候选有一条绑定其快照哈希的 `pass` 复核记录，并且 `report` 成功 |
| `completed_with_issues` | 最新候选的复核结果是 `issues`，并且修复轮次已用完；或者 token 预算在修复过程中耗尽，但已经存在至少一个复核过的候选 |
| `review_unavailable` | 复核会话重试后仍失败 |
| `blocked_by_safety` | 现有安全检查触发硬停止，例如路径逃逸、隔离规则被篡改、凭据外泄检测 |

没有产出候选就停止、失败或耗尽预算的任务，保持现有的生命周期状态（`stopped`/`incomplete`/失败），`outcome` 为空。界面要分开展示两件事：结局（复核层面）和可比性（评测层面）。`verified` 的文案要写"已复核"，不能写"复现成功"。

### 5.7 删除传统模式

**前端**：删除 `features/guided`、`features/lab`、`features/papers`、`features/runs`、`features/reports`、`MockWorkspace.tsx`、`api-client/guided*`、`api-client/phase01*`、`api-client/mock`，以及 `app.tsx` 和 `routing.ts` 中的旧路由；删除对应的旧测试。`Dashboard`/`TaskDetail`/`Sidebar` 改为读取自主任务 API。新建任务流程放进自主界面：建项目 → 上传论文 → 解析（复用 docparse 组件）→ 配置与授权 → 启动。工作台时间线 `stages.ts` 改为直接展示数据库里的真实阶段、回退记录和复核轮次，不再从事件推断阶段。

**后端**：
- 整包删除 `p2r_runner`、`p2r_codegen`、`p2r_kernel`，同步更新 `LAYERS` 和三文件源码根。
- `p2r_sandbox` 只保留 `autonomous/` 子包和它依赖的文件；`sandbox.py`、`_winappc.py`、`_winjob.py`、`_landlock_launcher.py` 等旧进程沙箱模块删除。包仍然保持 standalone。
- `p2r_orchestration` 中只服务旧流程的模块删除，包括 `run_worker`、`report_builder`、`repair_repository`、`codegen_service`、旧 `service.py`、`environment_checker`、`extraction_service`、`lenient_extraction` 等。
- `p2r_api` 中只服务旧流程的路由删除，包括 `demo`、`codegen`、`runs`、`reports`、`sessions`、`experiments` 等，以及 `demo.py`、`demo_receipts.py` 和 `create_demo_app`。启动器改为使用 `create_app()`，并默认启用自主路由。
- **保留**：项目、论文上传与内容、docparse、认证、自主路由、设置相关接口。
- `paper_context()` 只读取经过校验的解析文本，不再读取 `ExtractionRecord`。

**数据库**：不删表，不删行，不做破坏性迁移。

**文档**：更新 README、AGENTS.md（目前写的是 Guided Demo 和旧证据类别）、`docs/architecture.md`；在 12/13 号文档开头注明"旧模式保留"这一条已被本设计取代。

### 5.8 Windows

执行环境：Docker Desktop（WSL2 后端）运行同一个 Linux 容器。容器内的 `iptables`、`fcntl` 等跑在 Linux 中，不需要改。

宿主侧需要移植的地方：
- `research_capabilities.py:97` 的技能注册表锁和 `autonomous/service.py:231` 的任务租约锁：改用跨平台锁助手，POSIX 上用 `fcntl.flock`，Windows 上用 `msvcrt.locking` 或 `LockFileEx`。
- `p2r_evaluation/autonomous.py:88` 和 `research_capabilities.py` 中依赖 `O_NOFOLLOW`/`dir_fd` 的防符号链接读取：Windows 上要提供等价的安全语义，拒绝链接和重解析点，并确认最终路径仍在根目录内，**不能直接放宽**。
- `p2r_sandbox/autonomous/_interfaces.py` 的宿主网卡枚举，用来把宿主地址加入容器的拒绝列表：Windows 上用 `GetAdaptersAddresses`（ctypes）实现。
- `apps/desktop/main.cjs:182` 去掉对 Windows 的拒绝；启动时检查 `docker info` 的 OSType 是否为 linux，以及 Docker 是否可用，不满足时给出中文指引（安装 Docker Desktop、开启 WSL2）。
- `scripts/launch.py` 和 `启动Paper2Run.bat` 改为在 Windows 上走自主模式。
- Windows 宿主路径挂载进容器、SSH 目标使用 Windows OpenSSH：由实现者验证，不能工作的部分如实列为限制。

### 5.9 补充规则（builder check 后由设计者决定，标注为设计者的决定）

1. **证据版本按轮次区分**（设计者决定）。现有规则是"同一协议版本内产物名称不可变"（`evidence.py:46`，存储在 `artifacts/<protocol_sha>/<name>`），改为"同一协议版本、同一轮次内名称不可变"。存储路径改为 `artifacts/<protocol_sha>/r<round>/<name>`，256 个的上限按轮次计算。回退到 `paper_analysis` 时同样进入新的轮次目录。`evaluate_artifacts`、`verified_files` 和导出只读取当前候选所属的轮次。旧数据没有轮次目录，仍可只读。
2. **主 Agent 中途结束会话**（设计者决定）。如果会话以 `result` 结束，但还没有到达终态，Python 会以续写提示自动继续同一个会话。提示内容包括当前阶段、尚缺的交付物和允许的工具。连续 3 次续写都没有任何进展（没有新的已登记产物、没有阶段推进、没有作业结束），任务就以 `incomplete` 结束，`outcome` 为空。续写同样消耗 token 预算。只有到达 `report` 阶段并成功生成报告，才会走原来的报告逻辑。
3. **修复轮次用完时**（设计者决定）。达到上限后，`request_rollback` 会被拒绝，并提示"轮次已用完，请推进并提交候选"。如果复核结果是 `issues` 且轮次已用完，结局为 `completed_with_issues`。如果在任何候选产出之前预算就耗尽了，任务保持原来的生命周期状态，`outcome` 为空。
4. **复核者只读冻结副本**（设计者决定）。`submit_candidate` 冻结快照时，会把快照引用的产物和正式运行作业的日志一起复制到宿主侧的证据存储。复核者的 `read`/`logs`/`status` 不连接实验容器，而是指向一个新的只读目标，只能访问这份冻结副本和作业记录。因此，复核结果只针对快照里的内容。
5. **复核消耗计入任务预算**（设计者决定）。复核的 token 和时间都计入同一个任务预算。如果预算在复核过程中耗尽：之前已有复核过的候选时，结局为 `completed_with_issues`；没有时，结局为 `review_unavailable`，原因记为 `budget_exhausted`。
6. **`blocked_by_safety` 的触发条件**（设计者决定，请重点审阅）。只在一种情况下触发：容器隔离完整性校验失败，也就是 iptables 规则被篡改，或宿主/元数据地址可以访问。这种情况第一次发生就直接终止，不能恢复，因为容器已经不可信。现在这类错误只会让任务暂停，这里改为终止。`SECRET_IN_ARTIFACT`、路径逃逸等仍然按单次调用拒绝，不会让任务终止。
7. **迁移前的自主任务**（设计者决定）。`stage` 为空的旧自主任务不能恢复，控制接口返回"该任务创建于阶段化 harness 之前，请新建任务"，但仍可只读导出。`dispatch()` 遇到 `stage` 为空的任务，一律拒绝工具调用。

### 5.10 实施顺序

在新分支 `feat/autonomous-only` 上依次进行，每一阶段独立提交并通过测试后再进入下一阶段：
1. 阶段状态机 + 复核/修复 + 结局（后端，pytest 覆盖）
2. 前端改为只有自主模式，新建任务流程，时间线展示真实阶段
3. 删除传统模式的前后端代码和文档
4. Windows 移植

## 6. 交给实现者决定的事

- 新增字段放在列中还是 JSON 中；迁移的具体写法。
- 阶段说明和拦截提示的措辞；复核者系统提示词的具体内容。
- 复核会话复用 `AutonomousBridge` 的方式，以及同步复核期间 harness 工具调用的超时设置（必须大于复核超时）。
- 跨平台锁助手放在哪个模块（要符合分层规则）。
- 旧路由/模块删除清单的最终确认：先用 grep 确认没有保留代码引用它们，再删除；如果发现自主模式确实依赖某个模块，就保留并在提交说明里写明。
- 新建任务界面的布局细节（沿用现有设计体系）。

## 7. 验收

- pytest：每个阶段的工具拦截；每个检查的通过/拒绝；新鲜度规则；回退与轮次计数；pass/issues/轮次耗尽/复核失败四条分支；复核恢复的幂等性；`blocked_by_safety`。复核模型和主模型都用测试替身。
- 两个不变量测试通过；前端 `typecheck` 与 `test` 通过。
- 真实端到端：至少一个任务用真实模型和 Docker 走完 8 个阶段，记录在 13 号文档或新的验收记录中。
- Windows：有实机时完成一次端到端运行；没有实机时如实标注"未实机验证"，并给出单元测试覆盖范围。
