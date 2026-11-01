# Paper2Run 六步向导与本地纵向闭环设计

日期：2026-09-27
状态：对话设计已获用户明确批准；本规格待审阅。
工作区：`.worktrees/phase0-phase1`；分支：`feat/paper2run-phase0-phase1`。

## 1. 目标与边界

将复杂控制面转为 Paper → Extraction → Experiment → Run → Report → Export 的单主线体验。每一步只突出必要输入、当前状态和一个主操作；成功后用轻量动画过渡。交付的是连接真实本地 API、持久化对象及可校验文件的受控工程演示，不是完整论文复现。

不 commit、不 push、不安装依赖、不访问外网、不部署外部服务。不调用真实模型、GPU、真实训练或外部 Runner。保留已有无关修改。SQLite 验收不证明 PostgreSQL 锁、多 Worker 并发或生产安全。

首轮包含：固定受支持示例、证据展示、实验 revision/confirmation、本地 controlled smoke、真实事件、Report 状态、JSON/Markdown/ZIP 导出及刷新恢复。任意论文的语义解析、完整修复链、Runner reconciliation/reaper、生产鉴权矩阵及 PDF 渲染不纳入本轮；这些待办不因本轮而取消。

## 2. 现状核对与必须补齐的断点

- `apps/web/src/features/lab/Phase01Lab.tsx` 当前只有 Session/Cell/Checkpoint/Restore 工作台；不能直接将这些对象重命名为产品步骤。
- `apps/web/src/api-client/phase01.ts` 已具备错误 envelope 和写后刷新失败语义，可复用原则，但缺少产品链路客户端。
- Paper API 接收 PDF；当前确定性提取器仅提取 literal strings，语义字段缺失、candidate_experiments 为空。实验创建要求候选已在 Extraction 中持久化，故必须提供显式固定 Recipe/示例来源，不能由浏览器伪造候选。
- Report GET 当前无报告时返回 REPORT_NOT_READY，缺少完整构建 operation；JSON Export 已排队，Markdown/ZIP 尚不支持。
- operation 查询当前只解析 Paper 权限，扩展报告 operation 时须根据资源类型解析 Run/Project 权限，不能复用错误归属。
- Artifact download 当前固定 JSON 文件名；多格式交付需要正确 MIME、扩展名及 bytes/hash 校验。
- 依赖为 React、React Router、React Query、TypeScript、Vite；动画用 CSS，不引入新依赖。

## 3. 方案选择与界面

采用单页六步向导，不采用仅给旧控制面换皮，也不在首轮拆成六个独立页面。步骤状态与服务端资源状态分离，避免动画或组件挂载触发业务写入。

界面使用白/近白背景、黑色主按钮、灰阶文本、细边框、约 12px 圆角及充分留白。主内容单列；顶部显示六步进度及 Local demo 标识。已完成步骤可回看，尚未满足前置条件的步骤不可跳过。Events、Attempt、Execution、hash 放在详情区域；旧 Session 工具保留为高级入口，不删除既有能力。

切换采用 220–300ms 淡入与轻微位移。支持 prefers-reduced-motion、键盘操作、可见焦点、aria-live 和错误焦点。动画结束不决定业务成功；网络慢时显示真实等待状态，不模拟百分比。

## 4. 六步行为

### Paper

默认选择本地预置、受支持的示例，展示标题、来源及处理范围。优先复用仓库可用示例；若无真实授权论文素材，使用明确标注为教学 fixture 的本地文本示例，不冒充真实论文或伪造 PDF。第一阶段不承诺任意上传论文成功，也不增加无对应 API 的来源输入框。

用户确认使用示例后创建或关联真实 Paper 与项目记录。固定 demo 的初始化仅存在于显式开发模式，禁止默认在普通服务启动时注入演示资源。

### Extraction

展示持久化提取结果、引用片段及来源 hash。固定 Recipe 的人工配置与从原文提取的事实分别标注；缺失字段显示缺失，不补成虚构研究结果。只有已登记的受支持候选可进入 Experiment。

提取任务可以自动运行，用户仍点击“确认提取结果”继续，避免还未阅读证据就被自动跳走。无法验证页码或定位时不显示推测页码。

### Experiment

显示固定 Recipe、smoke 模式、预算及最少必要参数。只暴露后端实际支持和验证的字段；不添加无持久化支持的实验名称字段。内部按顺序创建 Experiment、Spec Revision、Confirmation，每个写操作独立幂等。

用户确认配置与预算后进入 Run 待启动状态。不得静默接受 blocking issues。返回修改产生新 revision/confirmation；旧 Run 和报告保留，不覆盖。

### Run

用户明确点击“开始本地 smoke”。服务端通过既有控制面执行固定、可信的本地任务；不执行用户任意代码或真实训练。展示 queued/running/terminal 状态、实际耗时、最近事件及取消请求。

结果以持久化状态为准。取消请求不等于进程已终止；失败或取消停留并显示原因，不自动创建新 Run。事件以 Run ID 和序号去重，断线后读取快照恢复，不将有限 SSE 快照宣传为常驻流。

### Report

受控任务的评测结果必须有明确来源：真实 smoke 检查或标注清楚的固定 fixture 指标，不能当成论文性能。Run 成功后进入报告阶段并发起一次报告构建；刷新仅查询已有 operation。

展示状态 pending → generating → ready/failed，由持久化 operation/报告记录决定。失败重试为显式用户操作；未知写结果先查询，不盲目新建。GET 不触发构建副作用。构建保存 Run、revision、Recipe、指标来源、限制、证据引用和安全错误码。

### Export

报告就绪后提供 JSON、Markdown、ZIP 的显式生成/下载操作。报告 ready 后停留展示摘要，用户点击继续进入 Export，防止自动过渡掩盖结果。

导出按报告内容版本/hash、格式及打包规则识别复用对象；同 key 同请求返回同结果，同 key 不同请求返回冲突。成功项可复用；混合格式失败显示逐项状态，不隐藏已成功文件。ZIP 仅打包当前 Run 有权限、已登记、hash/bytes 校验通过的报告、Manifest、Metrics 及允许公开的证据。安全相对文件名，拒绝路径穿越；不把整个工作目录打包。

必要证据缺失或损坏时，ZIP 不得宣称完整成功。Manifest 未 finalized 时保留 provisional 标识。PDF 首轮不提供成功下载，若请求则返回明确 PDF_UNAVAILABLE/REPORT_BUILD_FAILED，不安装渲染依赖或伪造文件。

## 5. 组件和后端职责

- 向导 shell：步骤导航、过渡、焦点与详情区域。
- 步骤组件：只展示数据并发出用户意图，不直接编排跨步骤写入。
- 流程 controller/reducer：资源 ID、前置条件、恢复与写操作状态。
- 产品 API 适配层：DTO 验证、envelope、分页与错误映射；不把 Session DTO 冒充 Run DTO。
- 本地 demo adapter：持久化固定示例和受支持候选，调用已有实验/执行业务路径，不暴露通用绕过权限接口。
- Report builder 与 export builder：独立于 HTTP 路由，复用现有 Job/Artifact 机制；路由保持权限和幂等职责。

仅统一此链路用到的 DTO 与分页行为。opaque cursor 原样传递、不在浏览器计算；Artifact 文本 offset/next_offset 保持 UTF-8 字节语义。全项目分页迁移不夹带进入本次 UI 改造。

## 6. 状态恢复和错误处理

步骤视图状态为 idle/working/complete/failed，另有写入结果未知的提示状态。服务端资源为事实来源；本地仅保存非敏感资源引用与必要的操作标识，不保存 secret 或将客户端状态当作成功证据。

刷新用已保存的项目/资源 ID 查询并校验关联，恢复最近有效步骤；权限变化时清除当前视图并显示错误。不根据列表最后一条猜测当前 Run。切换项目或身份取消旧查询，忽略迟到响应。

所有写请求携带 Idempotency-Key。一次用户意图保留稳定 key 和请求体，显式重试未知操作复用；已成功写入而随后读取失败只允许刷新，不自动再写。逐步保存已创建资源，避免多阶段创建部分成功后重复创建整条链路。React effect/轮询不得无条件发起写入。

后退查看不更改数据；编辑上游内容后后续状态标记需重新确认，历史记录保持不可变。不存在资源与跨项目无权限均不得通过前端 mock 回退掩盖。

## 7. 安全与真实性

Provider Secret 不进入浏览器、Runner、日志、SSE、Artifact 或导出包；导出仅使用安全投影和白名单。所有读写保留项目权限检查。Artifact 内容寻址、不可变，下载时核验内容及元数据；Action/Event append-only。

全程显示 local demo / controlled smoke / not full reproduction。开发身份不是生产鉴权；未完成 sandbox、完整取消回收或生产并发验证的事实必须保留在说明中。

## 8. 验收

1. 从空的本地 demo 数据目录启动，用户可从第一步完成整条链，不手工填内部 ID、不直接修改数据库。
2. 每步只有一个明确主操作；Extraction 审阅、Experiment 确认和 Run 启动不被动画跳过。
3. 服务重启或页面刷新后能恢复实际资源状态；重复点击、未知写结果与写后刷新失败不产生重复资源。
4. 报告四种状态与失败重试有测试，GET 不创建构建任务。
5. JSON/Markdown/ZIP 可下载，内容关联正确，hash/bytes 与登记一致；坏 hash、缺文件和越权均有失败测试。
6. 包含失败 Run、取消、缺候选、权限变化、事件去重、上游修改失效及迟到响应测试。
7. 现有 Phase 0/1 和 Phase 0/2 测试继续通过；旧高级工具可用。
8. 用 `.venv/bin/python`/`.venv/bin/pytest`（PYTHONPATH=.）、现有前端 Node 测试、TypeScript、Vite build 与 git diff --check 验证；浏览器检查桌面/窄屏和 reduced motion。未执行项明确报告，不引用历史通过数冒充本轮结果。

## 9. 规格自审

已消除以下歧义：自动推进不等于跳过证据/预算确认；固定指标不等于真实复现；缺候选需要服务端 demo 支持而不是前端 mock；PDF 不作为首轮成功条件；旧控制面保留；本规格不承诺全项目 DTO/安全/Runner 收尾。

后续实现计划需先核对现有 worker、模型与公共契约，再确定最小迁移及分步测试清单。本文未修改运行时代码、未运行验证、未 commit/push。
