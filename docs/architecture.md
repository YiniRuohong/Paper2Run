# Paper2Run 软件架构（当前实现）

本文描述仓库里的代码结构。产品只有一条路径：自主模式，设计见 [只保留自主模式的设计说明](superpowers/specs/2026-11-05-autonomous-only-research-stages-design.md)，早期依据为 [12 号改进工作文档](engineering/12-Paper2Run自主实验框架改进工作文档.md)，实施与实际验收结果见 [13 号实施与验收文档](engineering/13-Paper2Run自主实验框架实施与验收.md)。旧的固定配方 / 引导模式已于 2026-11-05 删除（后端包 `p2r_runner`、`p2r_codegen`、`p2r_kernel`，旧进程沙箱，以及 demo、sessions、experiments、runs、reports 等路由）；[11 号统一工程设计](engineering/11-Paper2Run统一工程设计.md)只作为历史设计保留。数据库表与 Alembic 迁移全部保留，没有破坏性迁移。

## 1. 仓库布局

```
apps/web/            前端：React + TypeScript + Vite（总览、新建实验、任务工作台、设置）
apps/harness/        独立 Node Pi SDK 桥接（JSONL、自定义工具、会话与预算）
apps/desktop/        Electron 与独立 Python/Node/Harness 资源分发
resources/research-skills/  八类内置科研能力包
services/api/        唯一可部署服务：FastAPI 控制面 p2r_api（鉴权、项目、论文、docparse、自主路由；
                     create_app / create_local_app 两个应用工厂）
packages/            Python 库，每个目录恰好一个 p2r_* 包
  contracts/         L0 公共数据契约（错误信封、规范 JSON、自主任务配置）
  artifacts/         L1 内容寻址产物存储、导出 ZIP 校验
  persistence/       L1 SQLAlchemy 模型、仓储、Alembic 迁移、任务队列
  sandbox/           L1 Docker 执行目标 ExecutionTarget（本机或 SSH；可独立安装，零依赖）
  evaluation/        L1 可信评测：冻结协议、内置指标重算、参考值校验
  docparse/          L1 PDF 解析（在线 mineru.net / 离线 MinerU），不依赖其他 p2r_* 包
  orchestration/     L3 控制面：autonomous/（任务、候选、复核、驱动、证据）、docparse 任务、科研能力注册表
scripts/             一键启动 launch.py、桌面构建、实验镜像构建与 UI 验收
tests/               unit/ 与 integration/，含架构分层测试
tools/               自主模式验收、docparse 端到端、导出校验脚本
docs/                产品与工程文档（软件代码不放这里）
```

## 2. 分层与依赖方向

```
L4  p2r_api
L3  p2r_orchestration          只允许向下依赖
L1  sandbox docparse evaluation artifacts persistence
L0  p2r_contracts
```

规则由 `tests/unit/test_architecture.py` 强制：

- 每个包只能 import 层号**严格更低**的包；
- `p2r_sandbox` 与 `p2r_docparse` 不得 import 任何 `p2r_*`，保证可单独安装；
- 新增包必须登记层号，否则测试失败。

L2 目前没有包（原 `p2r_runner`、`p2r_kernel` 已删除），层号保留不变。

`tests/unit/phase01/test_packaging.py` 保证 `pyproject.toml` 的 setuptools 发现路径、pytest `pythonpath` 与 `scripts/launch.py` 的 `SOURCE_ROOTS` 三处一致。

## 3. 主流程

论文入口：项目下上传 PDF（`POST /projects/{id}/papers`）或粘贴文本（`/papers/text`）。PDF 由 docparse 任务解析为 Markdown（`/papers/{id}/parse`、`/papers/{id}/text`）；自主任务的论文上下文只读取经过哈希校验的文本来源或解析文本，不读取旧的抽取记录。

### 3.1 自主实验

新建实验（`#/new`）复用论文上传与版本，冻结项目配置、论文上下文、科研 Skills 和实验协议。`/api/v1/autonomous` 负责身份、版本冲突、任务控制与事件；`orchestration/autonomous` 持久化任务并启动独立 Node 进程。官方 Pi SDK 1.5.0 负责推理与工具循环；Python 只执行工具、记录观测、控制权限与生成报告。

模型凭据只在控制面内存及 SDK 进程认证层使用，不挂入实验容器。SDK 的文件、命令、后台任务、日志及采集能力均由自定义工具转给 `sandbox/autonomous/ExecutionTarget`。每个自主任务有独立容器与工作区；同任务的实验进程共享工作区。

Pi 自己决定科研步骤，程序不设阶段关卡，`note_progress` 只用于展示。程序只把守结果出口，链路是：候选 → 复核 → 结局。

1. 候选（`autonomous/candidate.py`）：`submit_candidate` 是唯一的证据关卡。要求协议已冻结；当前轮次已登记 code、environment、data、result 四类产物；有一次入口运行成功，且其作业启动序号晚于最近一次 code 登记。通过后在 `task_root/candidates/<sha>/` 下冻结快照（哈希校验过的产物副本和分页取回的作业日志）。候选待复核期间，`register_artifact`、`submit_candidate`、`propose_protocol` 被拒绝（`CANDIDATE_UNDER_REVIEW`）。证据按轮次存放：`artifacts/<protocol_sha>/r<round>/<name>`。
2. 复核（`autonomous/review.py`）：在提交候选的那一轮结束之后执行，不在工具调用内同步执行，因为 harness 单次工具调用上限是 300 秒。复核是新的 harness 会话，只有读取冻结副本的只读工具和 `submit_review`，不连接实验容器，允许与主 Agent 同一个模型。
3. 驱动（`autonomous/driver.py`）：每一轮结束后由 `after_turn` 决定后续。有待复核的候选就复核，失败重试一次。复核有问题且轮次未用完，就把 findings 放进续写消息交给 Pi 修复，上限为 `limits.max_repair_rounds`（默认 3，范围 0 到 10）。没有候选时发只陈述事实的续写，连续 3 次无进展则以 `incomplete` 结束，不产生结局。
4. 结局：`verified`、`completed_with_issues`、`review_unavailable`、`blocked_by_safety` 四种，由程序写入 `autonomous_tasks.outcome` 和 `outcome_snapshot_sha256`，评测结论始终是 `not_comparable`。预算耗尽时 `state` 保持 `stopped`，`outcome` 有值。隔离完整性失败（`isolation_changed`、`network_isolation_failed`、`host_addresses_changed`）直接得到 `blocked_by_safety`，任务终止且不可恢复。

`evaluate` 和 `report` 不再是工具，报告由程序在任务到达终态时生成。`harness_json` 为空的旧任务不能恢复（`LEGACY_TASK`），仍可导出；已有结局的任务恢复返回 `TASK_FINISHED`。

应用关闭暂停 Agent 决策，后台实验可继续。重开后显式恢复并按持久启动 ID 对账；停止任务清理实验进程树，不释放租用实例。协议超出授权范围的变更进入待审批状态，只有匹配决策 ID 的批准才能继续。
`POST /tasks/{task_id}/decisions/{decision_id}`（`approve` 或 `reject`，可附 `note`）显式处理待审批决策：批准应用新协议，拒绝保留当前协议并以用户消息告知 Pi，两者都按恢复的同一路径重新排队。

评测支持 accuracy、RMSE 和 MAE，控制面重算已冻结产物。首版没有独立隐藏标签托管，因此即使实验执行完成，也保留 `not_comparable`；模型叙述单独标记，不作为执行或复现成功证据。ZIP 包含配置、论文上下文、协议、Skills 快照、事件、产物和哈希清单。

## 4. 执行目标

详见 [components/sandbox.md](components/sandbox.md)。自主模式要求本地或 SSH 目标上的 Docker 与 `paper2run-experiment:1` 镜像。可信控制器配置实际网络规则，实验进程使用无能力的非 root 用户，禁止宿主、私网与元数据访问；隔离探针失败时阻塞，不降级到宿主 shell。后台 supervisor 保存状态、限制日志、独立实施时限，并支持断线查询和取消。SSH 使用严格 known_hosts 校验且不转发认证 agent。存储限额属于观测值，token 根据服务商用量限制后续请求，不宣称单次在途响应的硬上限。

实际本地 Docker 与本机 sshd 测试不等于远端 GPU 验收；用户尚未提供远程 GPU 目标。POSIX 安全文件读取与锁不可用的平台会明确拒绝自主操作，跨平台实机支持以 13 号验收记录为准。

## 5. 启动方式

- 一键：`启动Paper2Run.command` / `.bat` → `scripts/launch.py`，自动建 venv、构建前端、单端口提供 API 与页面。
- 开发：`.venv/bin/python -m uvicorn p2r_api.main:create_local_app --factory --host 127.0.0.1`（需先 `uv pip install -e .` 或设置 `SOURCE_ROOTS` 对应的 `PYTHONPATH`）+ `pnpm --dir apps/web dev`。
- `create_local_app` 是单用户本地运行时：无登录，固定本地用户 `demo-user`（ID 沿用旧值，既有数据归属不变，首次启动自动创建），数据目录 `P2R_DEMO_DIR`（默认 `.p2r-demo`）。`create_app()` 挂载相同路由，默认要求登录会话。
- 桌面：`apps/desktop/backend/serve.py` 在回环端口上以 `create_local_app` 提供 API 与页面，并拒绝非本机 Host/Origin。
