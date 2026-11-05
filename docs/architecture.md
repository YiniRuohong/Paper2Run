# Paper2Run 软件架构（当前实现）

本文描述仓库里的代码结构。固定配方模式保留；新增自主模式依据 [12 号改进工作文档](engineering/12-Paper2Run自主实验框架改进工作文档.md)，实施与实际验收结果见 [13 号实施与验收文档](engineering/13-Paper2Run自主实验框架实施与验收.md)。[11 号统一工程设计](engineering/11-Paper2Run统一工程设计.md)保留为固定配方阶段的历史设计，不代表自主模式限制。

## 1. 仓库布局

```
apps/web/            前端：React + TypeScript + Vite（引导流程、自主实验、配置与事件）
apps/harness/        独立 Node Pi SDK 桥接（JSONL、自定义工具、会话与预算）
apps/desktop/        Electron 与独立 Python/Node/Harness 资源分发
resources/research-skills/  八类内置科研能力包
services/api/        唯一可部署服务：FastAPI 控制面 p2r_api（HTTP 路由、鉴权、demo 装配）
packages/            Python 库，每个目录恰好一个 p2r_* 包
  contracts/         L0 公共数据契约（Pydantic DTO）
  artifacts/         L1 内容寻址产物存储
  persistence/       L1 SQLAlchemy 模型、仓储、Alembic 迁移、任务队列
  sandbox/           L1 不受信任代码隔离执行（可独立安装，零依赖）
  evaluation/        L1 可信评测器（与被评代码隔离）
  codegen/           L1 修复候选补丁：构建、哈希、路径白名单审计
  docparse/          L1 PDF 解析（在线 mineru.net / 离线 MinerU），不依赖其他 p2r_* 包
  runner/            L2 执行配方：fixture smoke、real_cpu_baseline、sandbox_python（含数据集）
  kernel/            L2 交互式 cell 执行内核（spawn 子进程）
  orchestration/     L3 业务编排：抽取、生成、运行 worker、报告、导出、受控 runner
scripts/launch.py    一键启动（.command / .bat 调用）
tests/               unit/ 与 integration/，含架构分层测试
tools/               评测基准 benchmark/ 与数据集生成脚本
docs/                产品与工程文档（软件代码不放这里）
```

## 2. 分层与依赖方向

```
L4  p2r_api ──────────────┐
L3  p2r_orchestration     │   只允许向下依赖
L2  p2r_runner  p2r_kernel│
L1  sandbox docparse evaluation codegen artifacts persistence
L0  p2r_contracts
```

规则由 `tests/unit/test_architecture.py` 强制：

- 每个包只能 import 层号**严格更低**的包；
- `p2r_sandbox` 与 `p2r_docparse` 不得 import 任何 `p2r_*`，保证可单独安装；
- 新增包必须登记层号，否则测试失败。

`tests/unit/phase01/test_packaging.py` 保证 `pyproject.toml` 的 setuptools 发现路径、pytest `pythonpath` 与 `scripts/launch.py` 的 `SOURCE_ROOTS` 三处一致，且 runner 数据集随包发布。

## 3. 主流程

### 3.1 固定配方模式

```
上传论文 → 抽取实验协议(orchestration.extraction_service, 模型 Provider)
        → 生成代码 + AST 白名单审计(orchestration.codegen_service)
        → 创建 Run → run_worker 选择配方(runner)
             ├─ offline_fixture（教学 smoke）
             ├─ real_cpu_baseline（本机 CPU 真实训练）
             └─ sandbox_python（模型代码进 p2r_sandbox 执行）
        → 独立评测(evaluation) → 报告(report_builder) → 导出(export_builder)
```

证据等级（`evidence_class`）由程序写入报告，模型无权决定：`controlled_smoke`、`real_local_cpu`、`sandboxed_model_code`。

### 3.2 自主实验模式

`#/autonomous` 复用论文上传与版本，冻结项目配置、论文上下文、科研 Skills 和实验协议。`/api/v1/autonomous` 负责身份、版本冲突、任务控制与事件；`orchestration/autonomous` 持久化任务并启动独立 Node 进程。官方 Pi SDK 1.5.0 负责推理与工具循环；Python 只执行工具、记录观测、控制权限与生成报告。

模型凭据只在控制面内存及 SDK 进程认证层使用，不挂入实验容器。SDK 的文件、命令、后台任务、日志及采集能力均由自定义工具转给 `sandbox/autonomous/ExecutionTarget`。每个自主任务有独立容器与工作区；同任务的实验进程共享工作区。

应用关闭暂停 Agent 决策，后台实验可继续。重开后显式恢复并按持久启动 ID 对账；停止任务清理实验进程树，不释放租用实例。协议超出授权范围的变更进入待审批状态，只有匹配决策 ID 的批准才能继续。

评测支持 accuracy、RMSE 和 MAE，控制面重算已冻结产物。首版没有独立隐藏标签托管，因此即使实验执行完成，也保留 `not_comparable`；模型叙述单独标记，不作为执行或复现成功证据。ZIP 包含配置、论文上下文、协议、Skills 快照、事件、产物和哈希清单。

## 4. 沙箱后端

固定配方模式见 [components/sandbox.md](components/sandbox.md)，沿用 seatbelt（macOS）、bwrap/unshare（Linux）及其探针。

自主模式要求本地或 SSH 目标上的 Docker 与 `paper2run-experiment:1` 镜像。可信控制器配置实际网络规则，实验进程使用无能力的非 root 用户，禁止宿主、私网与元数据访问；隔离探针失败时阻塞，不降级到宿主 shell。后台 supervisor 保存状态、限制日志、独立实施时限，并支持断线查询和取消。SSH 使用严格 known_hosts 校验且不转发认证 agent。存储限额属于观测值，token 根据服务商用量限制后续请求，不宣称单次在途响应的硬上限。

实际本地 Docker 与本机 sshd 测试不等于远端 GPU 验收；用户尚未提供远程 GPU 目标。POSIX 安全文件读取与锁不可用的平台会明确拒绝自主操作，跨平台实机支持以 13 号验收记录为准。

## 5. 启动方式

- 一键：`启动Paper2Run.command` / `.bat` → `scripts/launch.py`，自动建 venv、构建前端、单端口提供 API 与页面。
- 开发：`.venv/bin/python -m uvicorn p2r_api.main:create_demo_app --factory`（需先 `uv pip install -e .` 或设置 `SOURCE_ROOTS` 对应的 `PYTHONPATH`）+ `pnpm --dir apps/web dev`。
