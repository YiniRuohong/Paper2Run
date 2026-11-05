# Paper2Run 自主实验框架实施与验收

> 执行方式：按子系统分工实施，父会话负责集成与最终验收；不自动提交或推送。

**Goal:** 落实 12 号文档的自主实验首版，从论文输入到真实隔离执行、可恢复会话、可信评测和证据导出。

**Architecture:** 保留现有 React/Electron/FastAPI/SQLite。新增自主模式通过 Python orchestration 调用独立 Node Pi SDK 进程，SDK 使用自定义工具调用隔离执行层；模型密钥由父进程内存注入，实验容器不持有控制面凭据。

**Tech Stack:** Python >=3.11、Pydantic 2、SQLAlchemy、React/TypeScript、Node >=22.19、Pi SDK、Docker、OpenSSH。Pi 精确版本以 SDK 验证结果锁定，不用自研推理循环替代。

## 全局约束

- 12 号文档为需求权威；已有 docparse 工作区改动保留。
- 用户在 2026-11-03 明确暂不提供 SSH GPU 机器，先完成本地实现与测试。远程代码和受控测试仍在范围内；真实 GPU 验收不可标记通过。
- GLM-5.3-Flash · ZCode 的实际模型 ID 经本地 magpie 查询为 `zcode/GLM-5.3-Flash`。凭据仅临时进程内使用，不进入 Git、事件、报告、SDK session 或容器。
- 不使用记忆里的其他机器作为授权目标，不提交 Git。
- 实验执行必须探测隔离；公网开放时网络层拒绝私有地址和元数据。任何失败不得降级为宿主无限 shell。
- 旧模式、旧记录和安全白名单保留。新模式不依赖 recipe，不强制标准库或分类输出。
- 用户停止、预算耗尽、应用退出、断线恢复有不同语义。应用退出不宣称 Agent 继续决策。

## 1. SDK 与执行基础验证

负责文件：`apps/harness/package.json`、锁文件、`apps/harness/bridge.mjs`、`apps/harness/test/`；`packages/sandbox/p2r_sandbox/autonomous/`、`scripts/autonomous/`。

- [x] 锁定官方 SDK 版本、许可证、Node 要求，安装并读取实际声明。
- [x] SDK 使用隔离的资源加载器，不发现用户全局认证、工具或扩展；只注册宿主提供的工具。
- [x] stdin/stdout JSONL 协议支持 start/tool_result/steer/abort，输出 ready/event/tool_request/result/error。
- [x] 使用真实 GLM 工具调用、失败修复、会话保存/恢复、取消与预算验证 SDK 行为。
- [x] 本地 Docker 专用环境支持文件、短命令、后台任务、日志游标、取消与采集。
- [x] 验证依赖安装、公网访问、私有地址拒绝；测试任务去重、断线恢复、时限和进程树停止。

接口：Python `ExecutionTarget(config, state_dir)` 提供 `probe/prepare/execute/start/status/logs/stop/read/write/collect/close`。Node `start` 接受模型配置、session_dir、可选 session_file、工具 JSONSchema、system_prompt、预算与 prompt；通过 tool_request 将实际执行交回 Python，SDK 管理模型循环。

## 2. 配置、协议与能力快照

负责文件：`packages/contracts/p2r_contracts/autonomous.py`、`packages/evaluation/p2r_evaluation/autonomous.py`、`packages/orchestration/p2r_orchestration/research_capabilities.py`、`resources/research-skills/`。

- [x] ProjectConfig 严格验证版本、额度、目标路径、SSH 别名和凭据引用。
- [x] 本地相对路径按项目根解析，远程路径不在本机解析。
- [x] 实现实验协议冻结、方案版本及偏差记录。
- [x] 分类与回归评测均由可信函数重新计算，拒绝非有限数值与协议篡改。
- [x] 缺少可靠来源、划分、独立评测等证据时返回 not_comparable。
- [x] 预装八类科研技能；本地导入验证结构、路径、兼容性、版本，安装不授予权限。
- [x] 运行固定技能内容和哈希，更新不改变已运行任务。

## 3. 持久任务与 API 集成

负责文件：`packages/orchestration/p2r_orchestration/autonomous/`、`packages/persistence/` 新增自主任务模型与迁移、`services/api/p2r_api/routes/autonomous.py`、runtime 装配。

- [x] 版本化项目配置、任务状态、SDK 会话路径、用量、执行句柄和追加事件落盘。
- [x] API 复用身份、membership、CSRF/origin、幂等和冲突错误语义。
- [x] 提供模型 profile（密钥内存）、配置、skills、任务启动/读取/事件/指令/暂停/恢复/停止/导出。
- [x] 背景工作线程启动 SDK，工具绑定 ExecutionTarget；工具结果和错误保留事实证据。
- [x] 应用退出中止 SDK 调度但保留后台任务；重开将未结任务标为待恢复，不盲目重跑。
- [x] 产物登记、冻结评测、报告和 zip 清单均从实际文件/命令生成，不将模型文本当作执行证据。
- [x] 重启后凭据未注入时明确请求重新配置，不静默换模型或假报成功。

新增自主路由前缀 `/api/v1/autonomous`。DTO 使用 JSON envelope。集成前先冻结 routes 与前端消费的数据形状。

## 4. 界面与桌面交付

负责文件：`apps/web/src/features/autonomous/`、客户端、guided/settings 接入；`apps/desktop/` 和 `scripts/desktop/` 运行时分发。

- [x] 现有任务可选择自主模式，上传解析材料能进入真实 Harness 上下文。
- [x] 模型/目标/技能配置、预算和授权确认可操作，密钥不使用 localStorage。
- [x] 展示工具活动、用量、失败、暂停与恢复状态；提供停止与导出。
- [x] 桌面携带 Node/Pi 桥接和技能资源，启动时检查版本与 Docker 条件。
- [x] 历史任务与原引导流程可回归访问。

## 5. 验收矩阵

| 要求 | 证明方式 | 状态 |
| --- | --- | --- |
| SDK 真实工具循环与恢复 | 实际 GLM 会话、工具证据、重开追加指令 | 已通过，最终 smoke 21.38 秒；18 项本地测试通过，凭据与生命周期复审通过 |
| 真实数据与多文件依赖实验 | 容器运行、数据来源与哈希、依赖、结果 | 最终真实 GLM 全流程 PASS：442 行原始数据、89 行固定测试集、2 个代码文件，独立 RMSE=57.72384071963741；20 项 ZIP 内容及八项 Skills 快照校验通过，结论 not_comparable |
| 产品 PDF 解析完整性 | 原 PDF 字节哈希、页数及在线分块 | 真实 48/48 页、3 分块解析通过；修复 multipart 末尾 CR/LF 丢失，原始与产品 SHA256 一致；5 项边界测试和真实 HTTP 往返通过 |
| 网络与文件隔离 | 公网成功、私有/元数据拒绝、穿越与符号链接测试 | 重启隔离、地址漂移、策略篡改及真实 302 第二跳拒绝独立复审 PASS；每次操作复验容器代次、完整策略指纹和实时地址覆盖 |
| 后台生命周期 | 去重、丢响应对账、超时、取消进程树、重连 | supervisor 丢失清理、真正丢启动响应后新对象去重独立复审 PASS；真实 SDK/Docker API 与应用关闭重开两项通过（88.73 秒） |
| 通用评测 | accuracy 与 regression、冻结篡改、缺证据不可比 | 76 项契约/评测/能力测试通过；独立复审 PASS |
| Skills | 本地导入、禁用、版本固定、运行快照 | 实现与并发/路径安全复审通过；真实验收已接入八项技能及快照校验 |
| HTTP 鉴权与恢复 | integration tests、重启、幂等、事件游标 | 六项后端修复独立复审 PASS；新增预算边界两项 P2 亦独立复审 PASS，6 项回归及 6 个独立场景通过；后端最终定向回归 100 项通过 |
| 前端流程 | typecheck/unit/build 与浏览器实际操作 | 最终 206 项测试、typecheck/build 通过；真实 Chromium + API + Pi SDK + Docker 的 12 组完整操作通过，包含任务状态同步、精确审批 ID 拒绝/批准、停止和 ZIP 下载 |
| 桌面分发 | runtime 构建/资源检测与启动 smoke | 最终 darwin-arm64 .app 与 ZIP 已生成；37 项验收通过，103 个 Python/12 个前端文件与源码及 dist 哈希一致；53,619 个 ZIP 文件凭据审计无命中 |
| 既有行为 | Python 完整回归与前端测试 | 最终 Python 全量 1266 passed / 24 skipped / 1 xfailed（266.71 秒）；最终前端 206/206、typecheck/build 与 SDK 18/18 通过 |
| 执行层 opt-in 整组 | 显式启用 Docker 与受控本地 SSH | 父会话最终复跑 21 passed / 1 skipped（35.66 秒）；唯一跳过项为公共服务未返回 302，受控 302 独立证据另列 |
| SSH GPU 实机 | 用户提供目标后的真实任务与资源证据 | 用户暂不提供，不得标通过 |

## 6. 本地运行与复验

前置条件：POSIX 主机、可运行的 Docker、Python 项目环境、Node >=22.19。macOS 的 Docker 可由 OrbStack/Docker Desktop 提供；Windows 自主模式当前明确不可用，旧引导流程保留。

```bash
uv sync --extra test
npm --prefix apps/harness ci
bash scripts/autonomous/build-image.sh
pnpm --dir apps/web build
uv run uvicorn p2r_api.main:create_demo_app --factory --host 127.0.0.1 --port 8000
```

访问 `http://127.0.0.1:8000/#/autonomous`。通过现有论文流程创建项目并上传 PDF 或粘贴正文，完成解析后返回自主模式。选择论文版本，配置模型 profile、目标、Skills、预算和实验目标，确认授权后启动。模型密钥只保存在当前控制面内存，服务重启后需重新注入；不能以普通 API 的 `env:` 引用读取任意宿主变量。

暂停保留后台训练；应用退出停止 Agent 调度但不表示训练结束。重开后先查看已有作业，补充模型凭据，再显式恢复。协议超授权变更须批准当前决策 ID；普通恢复不能替代批准。停止仅针对任务作业，不释放云实例或终止平台计费。

真实验收使用 `tools/autonomous_acceptance.py`，凭据由调用进程的 `P2R_TEST_API_KEY` 提供，不放命令参数或源码。未指定 `--base-url` 时脚本启动独立数据目录的服务；8000 端口必须空闲。

```bash
.venv/bin/python tools/autonomous_acceptance.py \
  --public-download-proxy http://127.0.0.1:7890 \
  --output .p2r-e2e-autonomous/fullflow --pdf --max-model-tokens 1500000
```

`--public-download-proxy` 仅作用于原论文与独立参考数据的无认证下载，不传递模型密钥、API headers 或 cookies。无本地代理时省略。`--pdf` 验证产品在线 MinerU 解析、原 PDF 字节哈希、48 页完整分块；默认路径是外部 pypdf 提取后上传文本，不能称为产品 PDF 解析验收。

实验采用公开 Least Angle Regression 论文和 sklearn 原始 diabetes 数据；模型安装依赖、编写多文件、后台运行 StandardScaler + Lars，脚本独立核对数据、固定划分、RMSE、技能快照及 ZIP 清单。它是探索性回归验证，不宣称达到论文指标。

### 桌面安装与最终交付

Apple Silicon macOS 安装包：`apps/desktop/release/Paper2Run-autonomous-mac-arm64.zip`（268699833 字节，256.25 MiB）。解压后打开 `Paper2Run.app`。包内含 Python 3.12.16、Node 22.22.2、Pi SDK 1.5.0 和八项科研 Skills，无须另装 Python/Node。自主实验仍需启动 Docker，并在项目目录执行 `bash scripts/autonomous/build-image.sh` 构建实验镜像；模型和论文在线解析服务使用用户自己的配置。

安装包 SHA256：`e36352bdd958d1d4bf4dc0695b75860022fbb8ea2abd2ca55947ecdcdf231f06`。这是本机 ad-hoc 签名包，未 notarize；若 macOS 阻止打开，可在确认来源后通过系统“隐私与安全性”允许打开，不需要全局关闭安全保护。

最终源码保留在当前工作区，未提交或推送 Git。实现保留既有 docparse 改动、旧引导流程和历史任务；`Ali-Hackthon/` 既有目录未纳入本轮修改。

## 7. 本轮验收证据

- `.p2r-e2e-autonomous/fullflow/20261103-212459-1da02a72/result.json`：最终真实 GLM 全流程 PASS。真实产品 PDF 解析 48/48 页，任务 completed；独立来源数据/划分/指标检查、八项 Skills 内容与哈希、导出清单均通过。凭据扫描覆盖 6903 个文件、293324830 字节，无命中。
- 同目录 `evidence.zip`：20 项文件，SHA256 `714665e34b5d8a913373555273004e0603d172798388dbc2e43f2bdbcd41406d`；`independent-evaluation.json` 保存独立核验指标。
- `.p2r-e2e-audit/ui/20261103-213146/result.json`：最终浏览器 12 组断言通过，页面错误 0；包含运行/暂停/审批/停止时 dropdown 与详情状态同步。截图、trace 和实际下载 ZIP 在同目录。

- `.p2r-e2e-autonomous/python-regression-complete.log`：1266 passed、24 skipped、1 xfailed；跳过的可选 Docker/SSH 测试另行显式启用验收，不能将 skipped 算作通过。
- `.p2r-e2e-autonomous/execution-optin-final.log`：父会话显式设置 `P2R_DOCKER_TESTS=1 P2R_SSH_TESTS=1`，完整运行执行层 unit、Docker 和受控 SSH 集合，21 passed、1 skipped（35.66 秒）。篡改测试以 `iptables-save -c` / `iptables-restore -c` 保留计数，避免污染共享 fixture；生产隔离检查保持不变。唯一跳过项因公共服务返回 403 而未观测到 302。
- `.p2r-e2e-autonomous/web-final.log` 与 `sdk-final-regression.log`：最终前端 206/206 和 typecheck、SDK 18/18 通过；前端 build 另已进入最终桌面产物。
- `apps/desktop/.cache/final-desktop-tests-20261103.log`、`final-resource-verification-20261103.json`、`final-zip-verification-20261103.json`：37 项桌面测试通过，真实 Electron 启动/渲染/退出及端口释放通过；103 个 Python 文件、12 个前端文件与当前源码及 dist 哈希一致。ZIP 保留 27 个符号链接和 142 个可执行文件权限，CRC 通过。
- 父会话最终 ZIP 流式凭据扫描：覆盖 53619 个文件、706662848 解压字节，凭据匹配 0；CRC 通过，ZIP 哈希与上述交付值一致。最初按目录扫描触发 10000 文件上限，后改为完整 ZIP 流式扫描，不将扫描上限误报为凭据泄漏。
- `.p2r-e2e-audit/backend/budget-final-independent.log`：最终预算边界独立复验；最后获准响应保留为真实报告，任务仍 stopped；恢复对账首项耗尽即停止，无下一项普通 status 调用。

- `.p2r-e2e-autonomous/lifecycle-integration-final.log`：真实 Pi SDK + Docker 的 API→报告，以及应用关闭→训练继续→重开暂停→凭据恢复→显式继续→同 launch ID 去重→停止，2 项通过。
- `.p2r-e2e-audit/execution/final-life-c5a977f566/results.json`：重启后拒绝派发、supervisor 丢失后 guardian 清理、真正杀掉启动客户端后新对象对账。
- `.p2r-e2e-audit/execution/final-reverify-a7cd1c5510/results.json`：地址删除/已覆盖新增允许、未覆盖新增拒绝、前置 ACCEPT 篡改拒绝；正常短命令约 0.45 秒，不重复主动公网请求。
- `.p2r-e2e-audit/execution/controlled302-8d666a6d9f/results.json`：受控 HTTP 服务返回真实 302，客户端尝试 metadata 第二跳后被内核 REJECT，计数 1→2，策略指纹不变。公开重定向服务返回 403 的尝试不计为通过。
- `.p2r-e2e-autonomous/fullflow/20261103-211008-a9862c0b/result.json`：首轮完整 PDF 路径，训练及产物登记后预算停止，明确 FAIL；凭据扫描覆盖 6901 个文件、292132309 字节，无命中。此轮不能替代最终成功证据。

停止残留子孙采用每 job 独立非特权 UID 与 guardian；同任务工作区共享组和依赖环境。每次执行验证容器代次、完整防火墙指纹、实时宿主地址覆盖及初次探测记录；地址删除或已有拒绝网段内新增不误阻塞，未覆盖地址仍拒绝。真实地址枚举另有检查；审阅中的受控生命周期复现固定地址集合，不冒充真实地址稳定性证据。

## 8. 已确认的限制

- 未提供真实 SSH GPU 目标，不能用本地 sshd/Docker 测试替代远端算力验收。未知或变化的主机指纹拒绝连接。
- 存储额度目前为观测值；模型 token 用量依据 provider usage 限制后续调用，单次在途响应可能超限。
- 首版没有独立隐藏标签托管，即使执行完成也保持 `not_comparable`，模型自报 provenance 不构成正式比较证据。
- macOS arm64 桌面为本机 ad-hoc 签名构建，未 notarize；macOS x64/Windows 未实机验收，Windows 自主模式不受支持。
- 能力包原子发布和进程锁已验证；极端掉电后的目录 fsync 持久性不在当前保证范围内。

## 完成判定

逐项记录执行命令、结果和局限。测试替身只能证明接口或错误处理，不能替代实机证据。保持 12 号文档的原范围；任何未完成项继续推进，外部缺失项明确标注。同步修订与新能力冲突的历史文档，实际行为仍以实现和验收证据为准。
