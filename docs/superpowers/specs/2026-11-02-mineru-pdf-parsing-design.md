# MinerU PDF 解析集成设计

_日期：2026-11-02 · 状态：待用户审阅 · 分支基线：`feat/desktop-app` · 实现分支：`feat/mineru-pdf-parsing`_

## 1. 意图与动机

Paper2Run 的真实链路是「上传论文 → 模型抽取实验协议 → 生成代码 → 执行 → 评测」。目前**真实 PDF 无法进入模型抽取**：

- `packages/orchestration/p2r_orchestration/extraction_service.py:204` 对源字节直接 `content.decode("utf-8")`，二进制 PDF 必然抛 `ProviderError("invalid_response")`。只有粘贴文本 / 上传 `.txt` 能跑通。
- 无模型的确定性路径 `services/api/p2r_api/routes/papers.py:290` 只用正则抠 PDF 字面串，压缩流 PDF 基本抽不出内容。

目标用户不只是开发者本人：**任何人安装桌面版（或用一键启动脚本）后，都应能直接上传 PDF 论文并进入抽取流程。** 用 MinerU 把 PDF 转成带页码标记的 Markdown，作为模型抽取的输入。

## 2. 目标 / 非目标 / 反目标

**目标**

1. 首次上传 PDF 时让用户选择 **在线** 或 **离线** MinerU 解析，选择被记住，可在「设置」中更改。
2. 在线：调用 MinerU 官方免登录「Agent 轻量解析 API」，零安装即可用；超出单次限制的 PDF 自动本地切分 → 逐段上传 → 取回 → 拼合。
3. 离线：一键「下载」，在应用数据目录内安装独立的 MinerU 环境与模型，之后全程本机解析、不联网。
4. 两种方式产出同一形态的结果：带页码标记的 Markdown，供模型抽取，引用来源能落到真实页码（在线为段级、离线尽量页级）。
5. 解析状态如实展示（排队/解析中/完成/失败及原因），失败可重试。

**非目标**

- 不接入需 token 的「精准解析 API」（200 页版）。
- 不改进确定性正则路径（`_extract_pdf`）的抽取逻辑；它保留为无模型时的兜底。（上传页数改用 pypdf 后，它截取引用条数的上限 `paper.page_count` 会变准，这是唯一的连带变化。）
- 不做多轮分段抽取；超长文本按 5.3 的规则截断并如实告知。
- 不在 UI 渲染公式/表格/图片；只用 Markdown 文本喂模型。
- 不支持 Office/图片等其他格式，不做批量解析。
- 不调优 GPU/加速配置，交给 MinerU 自身的硬件检测。
- **Intel Mac（`darwin-x64`）永久不支持离线模式**（用户 2026-11-02 决定，今后也不支持）。该平台 `GET /docparse/status` 返回 `offline.state = "unsupported"`，UI 隐藏离线选项，只提供在线模式。

**平台范围**：离线模式支持并验证 macOS Apple Silicon（`darwin-arm64`）与 Windows x64（`win32-x64`），与 `.github/workflows/desktop.yml` 的构建矩阵一致。

**反目标（即使能跑也算失败）**

- 用户未选择在线模式时，论文被上传到任何第三方。离线失败**绝不**静默回退到在线；在线失败也不静默切换。
- MinerU 及其依赖（PyTorch 等）进入主程序环境，或把桌面安装包撑大到 GB 级。
- 解析失败或截断时报告/界面声称已完整解析。

## 3. 约束

- 分层 L0–L4 与三文件源根同步（`tests/unit/test_architecture.py`、`tests/unit/phase01/test_packaging.py`）必须保持通过。
- 网络请求只用标准库（`urllib`），与现有 `providers.urllib_transport` 一致；需带 `User-Agent: paper2run/0.1`（Cloudflare WAF 先例）。
- 后端只监听回环地址；出站请求仅限模型 Provider 与（用户选在线时）mineru.net。
- 桌面运行时：内置 CPython 3.12（python-build-standalone），以 `-I` 启动；`scripts/desktop/build_runtime.py:112-114` 当前裁掉了 `ensurepip` 与 `pip`。
- MinerU 4.x 需要 Python 3.11–3.14（满足）。
- 在线 API 限制（2026-11-02 核实，<https://mineru.net/apiManage/docs>）：免 token、按 IP 限频（超限 HTTP 429）；单文件 ≤10 MB、≤20 页；仅返回 Markdown（CDN 链接）；异步提交 → 轮询；文件上传为签名模式（POST 拿 `task_id` + `file_url` → `PUT` 文件 → `GET /api/v1/agent/parse/{task_id}` 轮询）。
- MinerU 许可证为 MinerU Open Source License（Apache 2.0 + 附加条款）。本设计不分发 MinerU，由用户机器按需下载。

## 4. 方案比较（已选定）

**离线安装方式**

| 方案 | 做法 | 取舍 |
|---|---|---|
| **A. 按需独立环境（已选）** | 点下载 → 用后端解释器在数据目录建独立 venv → pip 装 MinerU → 下载模型 → 解析时子进程调 `mineru-kit parse` | 安装包不变大，依赖隔离；首次需联网，安装失败需可清理重来 |
| B. 装进主程序环境 | 直接 pip 到主运行时 | 内置 Python 在 .app 包内不应改写；依赖可能与 FastAPI/pydantic 冲突，装坏全应用崩 |
| C. 预打离线包 | 我们按平台构建并托管「环境+模型」压缩包 | 最稳，但每平台几 GB 的构建与托管在比赛周期内不现实 |

**在线切分方式**

| 方案 | 取舍 |
|---|---|
| **本地用 pypdf 切分（已选）** | 纯 Python、BSD 许可、体积小；一次解决 20 页与 10 MB 两个限制；顺带给出可靠页数 |
| 同文件 + `page_range` 多次提交 | 省一个依赖，但官方未写明能绕开 20 页检查（仅错误码说明暗示），且 >10 MB 文件仍需切分 |

用户选择：离线 A + pypdf 切分。

## 5. 设计

### 5.1 新包 `p2r_docparse`（L1）

`packages/docparse/p2r_docparse/`，不导入任何 `p2r_*`（与 `p2r_sandbox` 同为独立包），只依赖标准库与 `pypdf`。纯「PDF 字节 → 解析结果」能力，不碰数据库与产物存储。

- `chunking`：用 pypdf 读页数并切分。规则：每段 ≤20 页且 ≤ `9.5 MB`（留余量）；某段超大小则对半再切，直到单页；单页仍 >10 MB → 该论文在线解析失败，错误码 `DOCPARSE_PAGE_TOO_LARGE`，提示改用离线。
- `online`：Agent 轻量 API 客户端。逐段串行：提交 → PUT 上传 → 轮询（间隔 2–3 s，单段超时默认 5 min）→ 下载 Markdown。429 / 5xx 指数退避重试（上限次数可配）。安全：签名上传 URL 与 `markdown_url` 必须为 `https`，主机名属于 MinerU/OSS 允许列表（如 `*.mineru.net`、`*.openxlab.org.cn`、`*.aliyuncs.com`，构建者以实测响应为准收紧）；下载体积上限（默认 8 MB/段）；不跟随到非允许主机的重定向。
- `offline`：子进程调用 venv 内 `mineru-kit parse`，字面量 argv、无 shell；环境变量白名单（`PATH`、`MINERU_HOME`、`MINERU_TELEMETRY_DISABLED=1` 等，不传任何密钥）；独立临时工作目录；超时默认 20 min 可配；整进程树击杀（POSIX 进程组；Windows 用 `CREATE_NEW_PROCESS_GROUP` + `taskkill /T /F`，本包独立，不复用 `p2r_sandbox` 的 Job Object 代码）。
  - **不联网的保证级别（尽力而为，非 OS 级）**：不传 `MINERU_API_KEY` 等任何远程凭据、从不加 `--remote`、设置 `HF_HUB_OFFLINE=1` / 模型源离线变量，防止解析时意外下载。MinerU 的远程解析必须显式 `--remote` + key 才会上传，因此论文外泄路径被配置封死；但本次不在 OS 层强制断网（MPS 在 seatbelt 下能否运行未验证）。UI 文案写「离线模式不会上传论文」，不写「系统级断网」。
- `install`：离线安装器（见 5.4）。
- `merge`：把各段 Markdown 拼合，每段前插入页码标记 `<!-- p2r:pages 21-40 -->`（离线如能拿到页级信息则用 `<!-- p2r:page 21 -->`）。
- 统一结果类型 `ParseResult`：`markdown`、`backend`（`online_agent` / `offline_mineru`）、`backend_version`、`page_count`、`pages_parsed`、`chunks`（各段页码区间与状态）、`warnings`。

注册：`test_architecture.py` 的 `LAYERS` 加 `"p2r_docparse": 1`，并加入 `STANDALONE`；三文件源根同步加入 `packages/docparse`；`pyproject.toml` 依赖加 `pypdf`（锁定主版本）。

### 5.2 数据与流程

- 新表 `paper_texts`（Alembic `0019`）：`id`、`paper_id`、`source_sha256`（PDF sha）、`text_sha256`（Markdown 内容寻址键，存入现有 artifact store）、`backend`、`backend_version`、`page_count`、`pages_parsed`、`chunk_map_json`、`status`（`parsing`/`ready`/`failed`）、`error_json`、时间戳。
- 新任务 kind `paper_parse`。PDF 上传成功后：若已选模式，自动入队，`papers.status` → `parsing`（该状态已在约束中）；未选模式则保持 `uploaded`，由前端弹窗选择后调 `POST /papers/{id}/parse` 入队。
- 解析用**独立 worker 线程**消费 `paper_parse`（单并发），不占用现有 guided-demo worker，避免长时间解析阻塞 Run/报告/导出。离线解析可能超过 60 s 租约，worker 需按 `JobRepository.heartbeat` 周期续租。启动时恢复过期租约。
- 完成：写 artifact + `paper_texts(ready)`，`papers.status` → `ready`；全部段失败：`paper_texts(failed)` + 固定公开错误码，`papers.status` → `failed`，可重试。
- **部分成功**：至少一段成功、有段在重试后仍失败时，状态为 `ready`，但 `warnings` 明确列出缺失页码区间（如「第 21–40 页解析失败，未包含」），并在拼合文本对应位置插入 `<!-- p2r:pages 21-40 missing -->`。UI 与抽取结果都显示该警告。「重试」只重做失败段，成功段的结果复用。
- **上传页数修正**：`routes/papers.py:25` 的 `_PAGE_RE` 对使用对象流压缩的 PDF（pdfTeX 默认输出，即多数 arXiv 论文）数出 0 页，从而误判 `PDF_INVALID`，MinerU 根本跑不到。上传时改用 pypdf 读页数；pypdf 无法打开 → `PDF_INVALID`。这是本功能能成立的前提，属本次范围。

### 5.3 抽取改动

- `routes/papers.py` 的 Provider 抽取分支：若论文为 PDF，读取其 `ready` 的 `paper_texts` Markdown 作为 `content` 传给 `ExtractionService.extract`；没有则返回 `409 PAPER_TEXT_NOT_READY`。文本论文路径不变。
- 溯源不变：`source_refs.pdf_sha256` 仍引用原 PDF sha（PDF 是事实源）；`context_hash` 覆盖实际发送的 Markdown。`extraction_payload.PROJECTION_METADATA_KEYS` 增加 `source_text_sha256`、`parse_backend`，让报告能追溯「抽取基于哪份解析文本、哪种后端」。
- 抽取系统提示补一句：正文中 `<!-- p2r:page(s) … -->` 标记给出页码，`source_ref.page` 取该段起始页。
- **超长文本**：`ExtractionService` 现在静默截断到 128 K 字符（`max_context_bytes`）。改为：先去掉参考文献节（`References`/`Bibliography` 标题到下一个同级标题或附录之前），保留正文与附录；仍超长则截断，并在抽取结果 `warnings` 中写明「仅前 N 页（按页码标记）发送给模型」，UI 显示。不改上限值。

### 5.4 离线安装

状态由安装器自身持久化在 `<docparse_root>/install-state.json`（不进任务表：它不属于任何项目实体），后台线程执行，同一时间只允许一个安装。

`docparse_root` = `P2R_DOCPARSE_DIR`，默认 `<P2R_DEMO_DIR>/docparse`；桌面版即 `<userData>/data/docparse`。

步骤（每步可报告进度、可取消、失败可整体清理重来）：

1. 预检：可用磁盘 ≥ 6 GB；Python 版本满足。
2. 建环境：`<后端解释器> -m venv --without-pip <root>/venv`。
3. 装包：`<后端解释器> -m pip --python <root>/venv/... install "mineru==<锁定版本>"`。索引默认 PyPI，失败或超时自动换国内镜像（如阿里云）；尊重用户已有 `PIP_INDEX_URL`。
4. 选档：以 `mineru-kit hardware detect --json` 的推荐为准（MinerU 规则：有 GPU/MPS → 全部档；纯 CPU ≥12 逻辑核 → 最高 standard，4–11 核 → basic，<4 核 → 仅 flash）。若推荐为仅 `flash`，不下载模型，安装界面明确告知「本机只能用极速解析 (`flash`)，表格和公式质量较低」。
   - 说明（已查证 <https://opendatalab.github.io/MinerU/usage/tiers/>）：PDF 的 `flash` 档是原生文本提取，无需模型；`mineru-kit parse` 对 PDF 默认 `standard`。论文超参多在表格中，`flash` 表格质量不足，因此默认仍下载模型。
5. 下载模型：`MINERU_HOME=<root>/mineru-home mineru-kit models download --tier <tier>` + `verify`。模型源若支持国内源（ModelScope）则同样做回退。
6. 冒烟：解析一份仓库内置的 1 页小 PDF，成功即 `ready`。

**桌面运行时改动**：`build_runtime.py` 不再裁剪 `pip`（`PRUNE_SITE` 去掉 `pip`），约增加 10 MB；仍裁 `ensurepip`（第 2 步用 `--without-pip`）。

**环境失效自愈**：venv 的 `pyvenv.cfg` 指向内置解释器路径，应用被移动/升级后会失效。每次解析前（及启动时）用 `mineru-kit --version` 快速自检；失效则仅重建 venv 与重装包（模型在 `mineru-home` 内保留，不重下）。

**卸载**：删除 `<root>/venv` 与 `<root>/mineru-home`，状态回到未安装。

### 5.5 API（`/api/v1`）

- `GET /docparse/status` → `{mode: null|"online"|"offline", offline: {state: "not_installed"|"installing"|"ready"|"broken"|"failed", step, progress, tier, version, bytes_on_disk, error}, online: {endpoint_reachable?: bool}}`
- `PUT /docparse/mode` `{mode}`（设置持久化在 `<docparse_root>/settings.json`）
- `POST /docparse/offline/install`、`POST /docparse/offline/cancel`、`DELETE /docparse/offline`
- `POST /papers/{id}/parse`（幂等键，与现有写接口一致）
- `GET /papers/{id}/text` → 解析状态、段进度、`pages_parsed/page_count`、Markdown 预览（截断）

公开错误码固定，不透出原始异常或第三方响应原文。

### 5.6 前端

- **首次选择弹窗**（上传 PDF 且 `mode` 为空时）：两张卡片。
  - 在线：零安装、马上可用；**明确写出「论文会上传到 MinerU 官方服务器（mineru.net）」**；超过 20 页会自动分段。
  - 离线：需下载约 1–3 GB（视机器），之后完全本机、不联网；CPU 上解析较慢。选中即开始安装并显示进度；安装完成后自动继续解析该论文。
- **设置页**新增「PDF 解析」分组：当前模式（可切换）、离线组件状态/档位/占用空间、安装/重试/卸载。
- **Guided 第 1 步**：PDF 上传后显示解析进度（如「解析中 · 第 2/3 段」）；完成后可展开查看解析文本预览；失败显示原因与「重试」以及（适用时）「改用离线/在线」按钮——切换必须由用户点击，不自动发生。抽取按钮在解析完成前禁用。

## 6. 测试与验收

**自动化（不访问外网、不下载模型）**

- 切分：页数/大小边界（恰好 20 页、21 页、单页 >9.5 MB、超大文件对半细分）。
- 在线客户端：本地回环假服务器模拟 Agent API，含 429 退避、`failed` 状态、超时、非 https / 非允许主机 URL 被拒、超大下载被截断拒绝。
- 拼合：页码标记与段顺序正确。
- 离线：假的 `mineru-kit` 脚本验证 argv、环境白名单、超时击杀、失效自愈；安装器状态机（取消、失败清理、重试）。
- 抽取：PDF 论文无解析文本 → 409；有解析文本 → Provider 收到 Markdown，`pdf_sha256` 不变，元数据含 `source_text_sha256`。
- 两个不变量测试通过；前端 `typecheck` 与 `node --test` 通过。

**真实 Windows 验证（tailscale 内网 Windows 机 `win`，由 Claude 经 SSH/WSL 执行）**

在用户内网的 Windows 机器 `win`（tailscale `100.90.199.48`，不是 `moer`）上验证。注意 WSL 本身是 Linux：在 WSL 里直接跑 Python 测到的是 Linux 路径，不是 Windows。因此测试必须调用 **Windows 原生**可执行文件（经 WSL 互操作调用 `/mnt/c/.../python.exe` / `powershell.exe`，或直接走 Windows OpenSSH），使用 `win32-x64` 内置运行时（或同版本 Windows CPython 3.12）。步骤：

1. 走真实离线安装器：建 venv → pip 装 MinerU → `hardware detect`（档位以实机硬件推荐为准）→ 下载并校验模型 → 冒烟解析。
2. 用安装好的环境解析一篇 >20 页的真实论文 PDF，断言输出含页码标记、页数正确。
3. 在线 Agent API 的一次真实分段解析（遇 429 允许退避）。
4. 进程树击杀：超时场景下确认无残留 `mineru-kit` 进程。

局限如实记录：通过 SSH 只覆盖后端安装与解析链路（API 调用），不覆盖 Windows 上的界面点击。测试产物放在该机器上的独立目录，结束后可清理，不改动该机器的全局 Python / 系统配置。

**人工端到端（Apple Silicon 本机，写入 `docs/verification/` 新记录）**

1. 在线：上传一篇 >20 页的真实 arXiv PDF → 自动分段解析 → 抽取 → 走完六步。
2. 离线：桌面版中点「下载」完成安装 → 断网 → 解析同一 PDF → 抽取成功。
3. 移动/重装应用后离线环境自愈。

## 7. 留给构建者的决定

- MinerU 的确切锁定版本（实测通过的 4.x 版本）。
- 离线输出取哪种格式以拿到页级信息（Content List / 结构化输出等），以实测为准。
- 在线允许主机列表的最终取值（以真实响应中的域名为准）。
- 镜像源具体地址与回退超时；模型源是否支持 ModelScope。
- 轮询间隔、超时、重试次数的默认值；UI 文案细节。

## 8. 我（Claude）替用户做的决定（请审阅）

- 在线模式引用来源精确到段，不按页逐个上传（避免请求量 ×20 触发限频）。
- 首次上传 PDF 时弹窗选择，之后可在设置改；已选模式时上传后自动解析。
- 解析用独立 worker 线程，不与现有 demo worker 共用。
- 离线安装状态存文件而非任务表。
- 桌面运行时保留 pip（约 +10 MB）以支持按需安装。
- 离线档位按 MinerU 自己的 `hardware detect` 推荐自动选，不让用户选档。
- 解析失败时提供「改用另一模式」按钮，但绝不自动切换。
- 部分段失败时论文仍可用于抽取，但缺失页码明确警告；重试只重做失败段。
- 上传页数改用 pypdf（修复压缩 PDF 被误判无效），纳入本次范围。
- 超长文本先去参考文献再截断，并写明哪些页发给了模型；不做多轮抽取。
- 离线「不上传」靠配置保证（不传凭据、不加 `--remote`），本次不做 OS 级断网。
- 默认下载模型；只有 MinerU 判定本机只能用 `flash` 时才跳过。
