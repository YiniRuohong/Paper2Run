# Paper2Run

把论文变成可执行、可核查的实验。

## 这是什么

Paper2Run 是一个在本机运行的自主实验工作台。上传一篇论文后，由 AI Agent 自行决定研究步骤，在隔离的 Docker 容器里写代码、跑实验。控制程序只把守结果的出口：冻结候选结果、重算指标、由新会话独立复核，最后由程序判定结局并生成报告。

它不是「任意论文一键复现」的产品，也不承诺 GPU 级别的复现。

## 做什么

1. 新建项目，上传论文（PDF 或文本）。PDF 由 MinerU 解析，可用在线 mineru.net，也可用离线版本。
2. 配置模型服务、执行位置和授权，启动任务。
3. Agent 在实验容器中阅读论文、编写代码并运行实验，登记代码、环境、数据、结果四类产物。
4. Agent 提交候选后，程序冻结快照，重算指标（accuracy、RMSE、MAE），再由一个只读的新会话独立复核。
5. 复核发现问题时，问题交回 Agent 修复，默认最多 3 轮（可配置为 0 到 10）。
6. 任务结束时，程序判定结局并生成报告。报告可在应用内阅读、导出 PDF，完整证据可导出为 ZIP（配置、论文上下文、协议、事件、产物和哈希清单）。

结局共四种，均由程序写入，不由模型决定：

- `verified`：复核通过
- `completed_with_issues`：完成，但复核仍有未解决的问题
- `review_unavailable`：复核无法进行
- `blocked_by_safety`：隔离完整性检查失败，任务终止且不可恢复

## 运行前准备

无论用哪种方式运行，都需要：

- **Docker**：实验在 Docker 的 Linux 容器里运行，本机（或 SSH 目标主机）上必须已安装并处于运行状态。
  - Windows：安装 Docker Desktop，启用 WSL2 后端，并切到 Linux 容器模式（托盘菜单显示「Switch to Windows containers」即为 Linux 模式）。BIOS 需开启硬件虚拟化（Intel VT-x / AMD-V），WSL2 可用管理员身份运行 `wsl --install` 后重启启用。
  - macOS / Linux：Docker Desktop 或 Docker Engine 均可。
- **模型服务**：一个可用的 OpenAI 兼容接口（地址、密钥、模型名），在应用的设置页里配置。没有它 Agent 无法工作。
- **磁盘空间**：至少 8 GB 可用空间。启用离线 PDF 解析时还需额外约 3 GB。
- **网络**：用当前 `scripts/autonomous/Dockerfile` 构建的实验镜像，容器内 apt / pip 默认走阿里云镜像源。Agent 若从 GitHub 拉代码，需要能访问 github.com，国内网络下通常要为 Docker 配置代理。模型服务和在线 PDF 解析需要能访问对应地址。

其余要求取决于运行方式：

| | 桌面安装包（Windows） | 桌面安装包（macOS） | 源码运行 |
|---|---|---|---|
| Python、Node | 已内置 | 已内置 | Python 3.11+；前端未构建时还需 Node 22.19+ 与 pnpm |
| 实验镜像 `paper2run-experiment:1` | 已内置，首次启动自动导入 | 需自行构建 | 需自行构建 |

构建实验镜像：

```bash
bash scripts/autonomous/build-image.sh
```

Windows 的 PowerShell 中直接执行：

```powershell
docker build -t paper2run-experiment:1 -f scripts/autonomous/Dockerfile scripts/autonomous
```

### 环境检查

应用「设置 → 环境检查」会逐项检测硬件虚拟化与 WSL2（仅 Windows）、Docker、实验镜像、磁盘空间、镜像源可达性，并给出每一项的处理方法。安装包内置镜像时，这里也可以手动重新导入。

没有 Docker，或 Docker 处于 Windows 容器模式时，应用仍然可以启动，但任务会在准备阶段失败；新建实验页的「执行位置能力」会显示缺少什么。

### 可选组件

- **离线 PDF 解析**：在设置页一键安装 MinerU（标准档约 2.8 GB，约 10 分钟）。Windows 上默认装到 `%PROGRAMDATA%\Paper2Run\docparse`，以避开中文用户名路径导致的加载失败；可用 `P2R_DOCPARSE_DIR` 指定其他位置。不安装时可用在线解析，或直接粘贴论文文本。
- **SSH 目标**：仅在要把实验放到远程主机上跑时需要。目标主机同样要有 Docker 和实验镜像，并且其主机密钥已在本机 `known_hosts` 中（严格校验）。

Windows 额外注意：本地工作区只接受盘符路径，不支持 UNC 网络共享；数据目录不要放在 OneDrive 等云同步目录里。

## 快速开始

### 桌面安装包

- **Windows**：运行 `Paper2Run-<版本>-win-x64.exe`。安装包为当前用户安装，可选择安装目录，默认在 `%LOCALAPPDATA%\Programs\Paper2Run`；杀毒软件扫描文件可能使安装耗时数分钟。首次启动时，后台会把内置实验镜像导入 Docker（约 1 分钟），可在「设置 → 环境检查」查看进度。
- **macOS**：打开 `.dmg`，把 Paper2Run 拖入「应用程序」，然后按上文构建实验镜像。

安装包未签名：macOS 首次打开需右键选择「打开」，或在「系统设置 → 隐私与安全性」中允许；Windows 的 SmartScreen 需点「更多信息」再选「仍要运行」。

数据与日志位置（应用内「设置」可直接打开）：

- macOS：`~/Library/Application Support/Paper2Run/{data,logs}`
- Windows：`%APPDATA%\Paper2Run\{data,logs}`

### 一键启动（源码）

macOS 双击 `启动Paper2Run.command`，Windows 双击 `启动Paper2Run.bat`，或直接运行：

```bash
python scripts/launch.py
```

脚本会创建 `.venv`，在需要时构建前端，从 8010 起选一个空闲的本机端口提供 API 与页面，并在健康检查通过后打开浏览器。数据保存在 `.p2r-app`。

### 开发模式

开两个终端，均只监听本机回环地址：

```bash
uv pip install --python .venv/bin/python -e .

P2R_DEMO_DIR=.p2r-demo-dev P2R_ALLOWED_ORIGINS=http://127.0.0.1:5173 .venv/bin/python -m uvicorn p2r_api.main:create_local_app --factory --host 127.0.0.1 --port 8010

P2R_API_TARGET=http://127.0.0.1:8010 pnpm --dir apps/web dev --host 127.0.0.1 --port 5173 --strictPort
```

启动前请确认端口未被占用。写请求的 Origin 必须与 `P2R_ALLOWED_ORIGINS` 中的某一项完全一致。

## 构建桌面安装包

`apps/desktop/` 是 Electron 桌面壳。安装包内置独立的 CPython 3.12、Node 运行时、Agent 进程（`apps/harness/`）、科研能力包和前端页面；Windows 安装包还内置实验镜像。应用启动后在本机回环随机端口拉起后端，退出时后端随之退出。

```bash
python scripts/desktop/build_desktop.py
```

- 只能在目标平台上构建：Windows 安装包须在 Windows x64 上构建，macOS 安装包须在 macOS 上构建。Agent 依赖原生模块，交叉构建会被脚本拒绝。
- 需要 pnpm、Node 和网络。国内网络下 Electron 相关下载容易超时，可设置 `ELECTRON_MIRROR` 与 `ELECTRON_BUILDER_BINARIES_MIRROR` 指向 npmmirror。
- 要让 Windows 安装包带上实验镜像，构建前先把 amd64 镜像存档放到 `apps/desktop/.runtime/win32-x64/image/paper2run-experiment-1.tar.gz`：

  ```bash
  docker save paper2run-experiment:1 | gzip > apps/desktop/.runtime/win32-x64/image/paper2run-experiment-1.tar.gz
  ```

- 常用参数：`--skip-web` 复用已构建的前端，`--skip-runtime` 复用已准备的运行时，`--dir` 只出解包目录、不出安装包。
- 产物在 `apps/desktop/release/`：macOS 为 `.dmg` / `.zip`，Windows 为 NSIS `.exe` / `.zip`。构建结束时脚本会校验安装包内的 Node、Agent、科研能力包与 Python 资源。
- CI（`.github/workflows/desktop.yml`）在 macOS 与 Windows 上构建并冒烟测试，不签名、不发布。

桌面壳本身的开发调试：`pnpm --dir apps/desktop install && pnpm --dir apps/desktop dev`，使用仓库的 `.venv` 与 `apps/web/dist`。

## 已知限制

- 没有独立的隐藏标签托管，评测结论始终是 `not_comparable`。模型的叙述单独标记，不作为成功证据。
- 实验在 CPU 上运行。容器 GPU 直通的代码存在但未验证，也没有在远程 GPU 上验收。
- Windows 已于 2026-11-07 在一台 Windows 11 实机上用安装包完成一次完整任务（fastText AG News，结局 `verified`）；从 Windows 连接 SSH 目标尚未验证。
- 本地使用 SQLite。PostgreSQL 是生产目标，尚未验证。

## 测试

```bash
.venv/bin/python -m pytest tests/unit -q
.venv/bin/python -m pytest tests/integration -q
pnpm --dir apps/web typecheck && pnpm --dir apps/web test && pnpm --dir apps/web build
```

真实 Docker 与 SSH 的集成用例需要设置 `P2R_DOCKER_TESTS=1`、`P2R_SSH_TESTS=1`。测试通过不等于完整交付，端到端实测范围以验收文档为准。

## 目录结构

```
apps/web/        前端（React + TypeScript + Vite）
apps/harness/    Agent 进程，基于 Pi SDK
apps/desktop/    Electron 桌面壳
services/api/    FastAPI 控制面，唯一可部署的服务
packages/        分层的 Python 库，每个目录一个 p2r_* 包
resources/       内置科研能力包
scripts/         启动、桌面构建、实验镜像构建与验收脚本
tests/           单元测试与集成测试
tools/           验收与校验脚本
docs/            产品与工程文档
```

包之间按 L0 到 L4 分层，只允许向下依赖，由 `tests/unit/test_architecture.py` 强制检查。

## 文档

- [软件架构](docs/architecture.md)：当前实现的分层、主流程与执行目标
- [自主模式实施与验收](docs/engineering/13-Paper2Run自主实验框架实施与验收.md)：实际验证过什么
- [文档索引](docs/README.md)

`docs/engineering/` 下编号较早的规划文档写于固定配方阶段，其中 Runner、配方、Guided Demo 相关内容已随旧模式删除，当前实现以架构文档为准。
