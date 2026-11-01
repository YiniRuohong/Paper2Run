# Paper2Run 软件架构（当前实现）

本文描述仓库里**已实现**的代码结构。规划中的生产架构（远程 GPU Runner、PostgreSQL、Worker 集群等）见 [engineering/11-Paper2Run统一工程设计.md](engineering/11-Paper2Run统一工程设计.md)，两者不要混读。

## 1. 仓库布局

```
apps/web/            前端：React + TypeScript + Vite（总览、任务详情、新建任务引导流程）
services/api/        唯一可部署服务：FastAPI 控制面 p2r_api（HTTP 路由、鉴权、demo 装配）
packages/            Python 库，每个目录恰好一个 p2r_* 包
  contracts/         L0 公共数据契约（Pydantic DTO）
  artifacts/         L1 内容寻址产物存储
  persistence/       L1 SQLAlchemy 模型、仓储、Alembic 迁移、任务队列
  sandbox/           L1 不受信任代码隔离执行（可独立安装，零依赖）
  evaluation/        L1 可信评测器（与被评代码隔离）
  codegen/           L1 修复候选补丁：构建、哈希、路径白名单审计
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
L1  sandbox evaluation codegen artifacts persistence
L0  p2r_contracts
```

规则由 `tests/unit/test_architecture.py` 强制：

- 每个包只能 import 层号**严格更低**的包；
- `p2r_sandbox` 不得 import 任何 `p2r_*`，保证可单独 `pip install packages/sandbox`；
- 新增包必须登记层号，否则测试失败。

`tests/unit/phase01/test_packaging.py` 保证 `pyproject.toml` 的 setuptools 发现路径、pytest `pythonpath` 与 `scripts/launch.py` 的 `SOURCE_ROOTS` 三处一致，且 runner 数据集随包发布。

## 3. 主流程

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

## 4. 沙箱后端

见 [components/sandbox.md](components/sandbox.md)。按 seatbelt（macOS）→ bwrap → unshare（Linux）→ none 选择，每项能力都由探针子进程实测后才上报为可用。

## 5. 启动方式

- 一键：`启动Paper2Run.command` / `.bat` → `scripts/launch.py`，自动建 venv、构建前端、单端口提供 API 与页面。
- 开发：`.venv/bin/python -m uvicorn p2r_api.main:create_demo_app --factory`（需先 `uv pip install -e .` 或设置 `SOURCE_ROOTS` 对应的 `PYTHONPATH`）+ `pnpm --dir apps/web dev`。
