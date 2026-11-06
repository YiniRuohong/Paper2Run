# Paper2Run 项目技术规划

_把论文，变成可执行、可核查的实验。规划基线：2026-09-19。_

---

## 📋 当前结论

推荐继续队友的 Paper2Run 方向，但把“任意论文自动复现”收敛为**受支持 CV 分类实验的证据驱动复现工作台**。保留四页面和蓝图视觉，优先实现真实执行、有限修复、独立评测与诚实报告。

仓库是一个**本地可运行的自主实验工作台**，只有一条产品路径：自主模式。建项目 → 上传论文（PDF 或文本）→ 解析（在线 mineru.net 或离线 MinerU）→ 配置与授权 → 启动任务。Pi（AI Agent）自行决定研究步骤，在 Docker 实验容器里写代码、跑实验；Python 控制面只把守结果出口：提交候选 → 冻结快照 → 评测 → 新会话独立复核 → 有限轮修复 → 由程序判定的四种结局（`verified`、`completed_with_issues`、`review_unavailable`、`blocked_by_safety`）。它不是任意论文复现产品，也不承诺 GPU 级复现。

旧的固定配方 / 引导模式（Guided Demo、`/lab`、Mock、配方 Runner、进程沙箱）已于 2026-11-05 删除。

已知限制：

- 实验执行需要本机或 SSH 目标上的 Docker 和 `paper2run-experiment:1` 镜像（`bash scripts/autonomous/build-image.sh` 构建）；用户尚未提供远程 GPU 目标，本地 Docker 测试不等于远端 GPU 验收。
- 本机没有独立的隐藏标签托管，评测结论始终是 `not_comparable`；模型叙述单独标记，不作为成功证据。
- 本地持久化使用 SQLite；PostgreSQL 是生产目标，尚未在本机验证。
- 模型调用需要用户自行配置可用的 OpenAI 兼容模型服务。

自主模式的实施与实测验收记录见 [13 号实施与验收文档](docs/engineering/13-Paper2Run自主实验框架实施与验收.md)。

## 🚀 快速启动

- **一键启动（推荐）：** macOS 双击 `启动Paper2Run.command`，Windows 双击 `启动Paper2Run.bat`。两者调用 `scripts/launch.py`：自动创建 `.venv`、必要时构建前端，然后从单一本地端口（8010 起）同时提供 API 与页面并打开浏览器；需要 Python 3.11+。
- **开发模式：** 分别启动 API 与前端 dev server，命令见下文 [本地开发与测试](#本地开发与测试)。

## 🪟 Windows

Windows 与 macOS、Linux 使用同一种执行方式：Docker 里的 `paper2run-experiment:1` Linux 容器。**Windows 按构造支持，只有在 macOS 上强制走 win32 分支的单元测试覆盖，尚未在真实 Windows 机器上验证。** 覆盖范围与已知限制见[设计文档 5.11](docs/superpowers/specs/2026-11-05-autonomous-only-research-stages-design.md#511-windows)。

前置条件：

- **Docker Desktop**，启用 WSL2 后端，并处于 Linux 容器模式（托盘菜单显示 "Switch to Windows containers" 即为 Linux 模式）。实验镜像需在该 Docker 中构建：在仓库根目录运行 `docker build -t paper2run-experiment:1 -f scripts/autonomous/Dockerfile scripts/autonomous`（即 `scripts/autonomous/build-image.sh` 的内容，PowerShell 中也可直接执行）。
- **Python 3.11+**（一键启动 `启动Paper2Run.bat`；安装时勾选 "Add python.exe to PATH"）。桌面安装包自带 Python，不需要单独安装。
- **Node ≥ 22.19**，或使用桌面安装包内置的 Node 运行时。

没有 Docker 或 Docker 处于 Windows 容器模式时，应用照常启动，启动日志给出中文提示；新建实验页的"执行位置能力"会显示缺什么。启动按钮不会因此被禁用，任务在准备阶段失败并说明原因。

限制：本地工作区只接受盘符路径（不支持 UNC 网络共享）；数据目录不要放在 OneDrive 等云同步目录（云文件占位符是重解析点，会被安全读取拒绝）；从 Windows 连接 SSH 目标依赖 Windows OpenSSH，未验证；不在 Windows 上验证 GPU。

## 🖥️ 桌面应用（macOS / Windows）

`apps/desktop/` 是 Electron 桌面壳：内置一份独立的 CPython 3.12（python-build-standalone，按 SHA-256 固定），启动时在本机回环随机端口拉起后端，并在原生窗口中加载界面；用户无需安装 Python 或 Node。之所以内置真实解释器而不是 PyInstaller，是因为 Electron 以解释器参数启动 `serve.py`，离线解析组件 MinerU 也要用 `sys.executable -m pip` 按需安装。

- **构建：** `python scripts/desktop/build_desktop.py`（默认本机平台）；`--target win32-x64` 可在 macOS 上交叉构建 Windows 安装包；`--skip-web --skip-runtime --dir` 用于快速出包。产物在 `apps/desktop/release/`（macOS：`.dmg`/`.zip`；Windows：NSIS `.exe`/`.zip`）。CI 见 `.github/workflows/desktop.yml`，在真实 macOS 与 Windows 上构建并冒烟测试内置运行时。
- **开发：** `pnpm --dir apps/desktop install && pnpm --dir apps/desktop dev`，使用仓库 `.venv` 与 `apps/web/dist`。
- **数据与日志：** macOS 在 `~/Library/Application Support/Paper2Run/{data,logs}`，Windows 在 `%APPDATA%\Paper2Run\{data,logs}`；应用内「设置」可直接打开。退出或应用崩溃时后端随之退出（stdin 看门狗）。
- **未签名：** 安装包未做代码签名/公证。macOS 首次打开需右键 →「打开」（或在「系统设置 → 隐私与安全性」中允许）；Windows SmartScreen 需点「更多信息 → 仍要运行」。

## 🗂️ 仓库结构

软件代码在 `apps/`（前端）、`services/api/`（唯一可部署服务）、`packages/`（分层 Python 库）、`scripts/`、`tests/`、`tools/`；所有产品与工程文档在 `docs/`，索引见 [docs/README.md](docs/README.md)，当前实现的分层架构见 [docs/architecture.md](docs/architecture.md)。

## 📚 文档导航

**总体工程规划：** [总体工程规划](docs/engineering/00-总体工程规划.md) 是总体工程文档入口；其中 [Paper2Run 统一工程设计](docs/engineering/11-Paper2Run统一工程设计.md) 是首版 Demo 的唯一工程总设计和优先级更高的设计基线。它统一定义 Paper2Run Framework Service、Pi SDK + Extension、OpenAI-compatible Provider、FastAPI 控制面、Worker、Runner、独立评测、公共 API 和内部 API 的边界。

**前后端交接标准接口：** [前后端交接标准接口](docs/engineering/00-前后端交接标准接口.md) 是两人联调入口；具体标准见 [前后端公共接口契约 v0.2](docs/engineering/09-前后端公共接口契约.md) 和 [发给队友的 GUI 交接说明](docs/engineering/10-前端GUI交接说明.md)。用户负责后端，队友完整负责 `apps/web/`；公共 API 字段以 v0.2 为准，旧 06 的公共示例不再作为独立标准。

[前端交接包 v0.2](docs/delivery/Paper2Run-前端交接包-v0.2.zip) 包含上述两份文档、原型 HTML 和阅读说明，可直接转发。它是本次基线快照，后续修改文档需重新出包；不包含任何密钥、账号或服务实现。

1. **[总体工程规划](docs/engineering/00-总体工程规划.md)**：总体工程文档入口；先读其中的统一工程设计。
2. **[前后端交接标准接口](docs/engineering/00-前后端交接标准接口.md)**：浏览器公共 API、DTO、SSE、Mock、页面状态和联调边界。
3. **[统一工程设计](docs/engineering/11-Paper2Run统一工程设计.md)**：首版唯一总设计；总体工程的最高优先级基线。
4. [总体技术架构](docs/engineering/02-技术架构.md)：产品范围、技术取舍、科研可比性与安全原则。
5. **[模块化与数据设计](docs/engineering/05-模块化与数据设计.md)**：模块职责、内部端口、表与约束、Experiment/Revision/Run/Execution 生命周期。
6. **[接口定义](docs/engineering/06-接口定义.md)**：请求/响应、错误码、分页、幂等、SSE、报告导出、Runner 内部协议。
7. **[运行与部署](docs/engineering/07-运行与部署.md)**：四种运行模式、进程与端口、配置、拟实现启动命令、两机部署、恢复与重放。
8. **[前后端公共接口契约](docs/engineering/09-前后端公共接口契约.md)**：浏览器 `/api/v1` DTO、页面状态和联调规则。
9. **[前端 GUI 交接说明](docs/engineering/10-前端GUI交接说明.md)**：页面、Mock、视觉和前端负责人边界。
10. **[开发任务包](docs/engineering/08-开发任务包.md)**：按依赖拆分的任务、允许路径、验收、测试与完成定义。
11. [比赛规则与交付清单](docs/engineering/01-比赛规则与交付清单.md)：公开证据、待官方核实条款和提交材料。
12. [实施计划与验收](docs/engineering/03-实施计划与验收.md)：日历排期、团队分工、基准设计与演示。
13. [技能安装与使用记录](docs/engineering/04-技能安装与使用记录.md)：来源、安装校验、许可证和限制。

阅读顺序建议：先读 00-总体工程规划；两人联调再读 00-前后端交接标准接口 → 10 → 09；后端实现按 11 → 02 → 05 → 06 的内部协议 → 07 → 08 查阅。公共接口 DTO 与 GUI 协作范围以 00-前后端交接标准接口、09/10 为准，Framework Service、Provider 和确定性控制面的统一边界以 11 为准。规划文档包含未实现的生产能力；本地 Demo 的实测启动方法与验收范围以验证记录为准。

02、05、06、07、08、09、11 等规划文档写于固定配方阶段，其中 Run、Runner、配方、Session 与 Guided Demo 相关的接口和模块已随旧模式删除；当前实现以 [docs/architecture.md](docs/architecture.md) 为准。

### 本地开发与测试

首次安装：`uv pip install --python .venv/bin/python -e .`（一键启动脚本会自动完成，并把全部源码根放进 `PYTHONPATH`）。在仓库根目录分别启动两个终端（仅 loopback，不部署）：

```bash
P2R_DEMO_DIR=.p2r-demo-dev P2R_ALLOWED_ORIGINS=http://127.0.0.1:5173 .venv/bin/python -m uvicorn p2r_api.main:create_local_app --factory --host 127.0.0.1 --port 8010
P2R_API_TARGET=http://127.0.0.1:8010 pnpm --dir apps/web dev --host 127.0.0.1 --port 5173 --strictPort
```

启动前确认端口未被占用，不要终止其他服务。`create_local_app` 是单用户本地运行时：无登录，所有请求以固定本地用户执行（首次启动时自动创建），数据放在 `P2R_DEMO_DIR`（变量名沿用旧名；默认 `.p2r-demo`，一键启动使用 `.p2r-app`）下的 `demo.sqlite3` 与 `artifacts/`。`create_app()` 挂载相同的路由，但默认要求登录会话，用于测试和非本地部署。自主模式的写请求必须带有与 `P2R_ALLOWED_ORIGINS` 中某一项完全相同的 Origin（逗号分隔，只接受 `http://127.0.0.1|localhost|[::1]:<端口>`，配置错误时启动即失败；未设置时为 `http://127.0.0.1:8000`）。开发模式下页面来自 Vite 的 5173 端口，所以上面的 API 命令设为 `http://127.0.0.1:5173`；一键启动和桌面应用会自动设为自己实际使用的端口。页面入口：`#/` 总览、`#/new` 新建实验、`#/task/<id>` 任务工作台、`#/reader[/<paperId>]` 论文阅读（PDF 预览与批注，批注只存在本机浏览器）、`#/settings` 设置。

测试：

```bash
.venv/bin/python -m pytest tests/unit -q          # 含分层与源码根一致性两项不变量测试
.venv/bin/python -m pytest tests/integration -q   # 真实 Docker/SSH 用例需 P2R_DOCKER_TESTS=1 / P2R_SSH_TESTS=1
pnpm --dir apps/web typecheck && pnpm --dir apps/web test && pnpm --dir apps/web build
```

不能把 HTTP 200 或测试通过视为完整交付；端到端实测范围以 13 号验收文档为准。

原始设计依据：[Paper2Run PRD](docs/product/Paper2Run-PRD.pdf) · [静态交互原型](docs/product/Paper2Run-原型.html)。原型中的分数、结果和曲线只是演示内容，不能作为实际实验或论文事实。

## 🎯 架构摘要

- **前端：** React + TypeScript + Vite：总览、新建实验、任务工作台、设置。
- **控制面：** FastAPI + Pydantic + SQLAlchemy（本地 SQLite），自主任务、事件日志（只追加）与 docparse 任务持久化。
- **Agent：** 独立 Node 进程运行官方 Pi SDK（`apps/harness/`），通过自定义工具调用控制面；模型走用户配置的 OpenAI 兼容服务，凭据不进入事件、产物和导出。
- **执行面：** 本机或 SSH 目标上的专用 Docker 容器，网络策略由容器内可信控制器实施并在每次操作前复核。
- **证据面：** 冻结的协议与候选快照、按轮次存放的产物、程序重算的指标、独立复核 findings 与哈希清单。
- **报告：** 结局由程序判定；评测结论保持 `not_comparable`，不生成误导性的论文吻合分。

最重要的设计变化：模型生成代码与解释，**程序决定能否执行、是否可比和实际指标是多少**。同一上传论文可以产生新的真实实验，而不是跳转到固定报告。

## ⚠️ 证据与交付状态

赛事官网本次出现登录和滑块验证，未取得完整第一方规则。公开转载支持“效率进化—Qwen 科研提效”方向，并记载 2026-10-07 招募/线上提交截止日；具体截止时刻、评分权重、材料规格、开源要求和既有代码政策仍需官方后台核实，详见规则文档。

内部排期采用 10 月 3 日功能冻结、10 月 5 日提交目标，均为建议，不是已发生的操作。工期、成功率、运行耗时和费用都没有实测保证。

本轮校验覆盖文档的相对链接、引用定义、JSON 示例和代码围栏，以及四个已安装技能的文件来源、哈希与 YAML 格式。产品功能、GPU 隔离和科研结果仍需按实施计划逐项验证。
