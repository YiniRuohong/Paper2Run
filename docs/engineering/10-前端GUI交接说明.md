# Paper2Run 前端 GUI 开发交接说明

_收件人：前端 GUI 队友 · 交接基线 v0.2 · 2026-09-19。项目发起人负责后端，你负责前端。本文可连同接口契约和静态原型直接转发。_

---

> **总设计优先级：** 本文只规定前端页面、Mock、视觉和联调边界；总体工程边界以 [Paper2Run 统一工程设计](11-Paper2Run统一工程设计.md) 为准。前端不实现 Provider、Framework Service、数据库、Docker、Runner 或评测器，也不把旧的 Qwen 专用适配器表述当作公共接口承诺。

## 📋 你需要知道的项目背景

Paper2Run 是论文实验复现工作台，不是聊天页面。用户上传论文，确认原文实验与本次参数，系统用 Qwen 生成代码并在隔离环境运行，记录修复过程，最后交付独立评测的结果和报告。

**当前没有可用后端，也没有 React 工程。** 已有的是 [静态原型](../product/Paper2Run-原型.html) 和设计文档。你的第一阶段目标是按 [公共 API 契约 v0.2](09-前后端公共接口契约.md) 做出可交互的 Mock GUI，之后同一套组件切到真实 API，而不是做只能演示固定成功数据的页面。

交接资料只需先读三项：

1. 本文：你做什么、哪些不做、页面与联调要求。
2. [公共 API 契约](09-前后端公共接口契约.md)：请求、响应、DTO、状态与错误。
3. [原型 HTML](../product/Paper2Run-原型.html)：四视图、布局、色彩和字体参考。双击即可看静态效果，但按钮大多没有真实功能。

其余后端架构资料不是你的开工前置。数据库、Qwen、Docker、GPU 和 Runner 的具体实现都由后端负责人承担。

## 👥 两人的责任与文件归属

### 你负责的前端范围

- React + TypeScript + Vite 工程、路由与四大产品视图。
- 登录/项目入口、上传、参数表单、原文定位、代码与 patch 只读预览。
- 任务状态、SSE/polling、取消交互、报告、曲线与下载入口。
- 空态、加载、失败、权限、过期会话、不可比结果、Mock/Live/Replay 标签。
- API client、字段类型消费、Mock、前端测试、响应式和键盘可达。

### 后端负责人交付给你

- 符合 v0.2 的公共 API 和同源代理可接入地址。
- 生成的 TypeScript 类型及 OpenAPI；当前文档 DTO 是实现前暂用契约。
- 可用测试邀请、owner/editor/viewer 账号场景，不在群聊发送模型 Key。
- 按阶段可调用接口清单和未完成项，不把 200 空壳说成已实现。
- 服务端字段校验、权限、任务状态、证据、指标/评分与真实下载。
- 联调错误的 request_id 查日志、版本冲突处理和服务启动说明。

### 文件边界与共享改动

前端独占 `apps/web/`，包括 src、入口、路由、Vite/TypeScript/Playwright 配置及本包 package.json；拥有自己的前端 fixtures 与测试。你可以独立调整这些文件，不必让后端代改 main.tsx。

后端独占 `services/`、Python `packages/`、数据库迁移、Runner、recipes、prompts、后端测试与部署。`packages/contracts/` 的 Pydantic/OpenAPI/生成 TS 由后端维护，前端只读消费。

根目录的 workspace 配置、Makefile、README、共享接口文档和根锁文件采用串行合并。前端提出新增依赖和版本，指定一人更新根 `pnpm-lock.yaml`，不能两边同时重生成锁。若没有共享前端 workspace，后续可由前端维护自己的锁，但必须先统一目录方案，不在此次交接里创建第二套锁。

**本节是当前明确两人分工，替代早期任务包里“前端入口也由 T01 代改”的 Agent 分工建议。** 不要为了遵守旧任务包而阻塞前端工程初始化。仓库当前未初始化 Git，文中的目录和协作分支都属于开发约定，不表示已创建。

## 🎨 视觉与组件交接

### 必须保留的设计语言

原型的 CSS `:root`（约第 8–30 行）是起点：

| Token | 值 | 用途 |
|---|---|---|
| `--bg` | `#0C2238` | 蓝图背景 |
| `--bg-deep` | `#081826` | 顶栏/终端底层 |
| `--panel` | `#10304E` | 面板 |
| `--line` | `#24486B` | 分隔线 |
| `--ink` | `#E9F1F9` | 主文字 |
| `--muted` | `#8FB0CC` | 次级文字 |
| `--paper` | `#F2ECDD` | 原文纸张区域 |
| `--amber` | `#F0A72E` | 主要操作/运行中 |
| `--green` | `#46C08A` | 完成/正常 |
| `--red` | `#E4606F` | 错误 |

保留衬线用于论文内容、等宽用于代码/日志/机器数据、系统无衬线用于操作界面。无需新增字体下载作为必需依赖。低对比小字可为可读性调整，不必机械复刻原型瑕疵。

原型 1080/860 断点可参考；重点验收 1440/1280 桌面和手机只读报告。窄屏三栏执行页应折成 tab/上下布局，不能整体水平滚动到看不到取消按钮。状态必须有文字/图标，不只靠颜色。

### 建议前端结构

```text
apps/web/src/
  app.tsx                    路由、Query provider、会话入口
  features/auth/             邀请登录与过期会话
  features/projects/         项目列表、上传、最近运行
  features/papers/            PDF 预览、证据卡片
  features/specs/             实验选择、草稿编辑与确认
  features/runs/              执行、文件、日志、patch
  features/reports/           指标、曲线、声明、导出
  api-client/                一个请求层和事件订阅层
  ui/                        按钮、标签、空态、dialog、错误提示
  formatters/                时间、费用、字节和指标单位
  styles/                    原型 tokens 与全局样式
  mocks/                     仅开发/测试启用的响应与场景
```

推荐 React Router + TanStack Query；代码显示使用只读编辑器/高亮组件，PDF 使用 PDF.js。具体依赖版本由两人锁定，不能直接复制上游 skill 中旧 API 示例。Mock 层可采用 MSW 或简单 adapter，但所有组件只通过同一个 api-client，不在组件里写固定返回数据。

## 🖥️ 页面逐项交接

### 1. 登录与工作台

建议路由 `/login`、`/projects`、`/projects/:projectId`。先 GET `/auth/me`，再 capabilities/projects；未登录显示邀请兑换，而不是直接报系统错误。

工作台包含：

- 项目选择/创建、论文 PDF 拖拽和文件选择、arXiv 输入。
- 明确的论文内容外传同意项，默认不勾；未同意不能上传。
- capabilities 给出的大小、页数和支持范围；不要写死“支持所有 CV/NLP”。
- 最近论文和运行列表，状态、耗时、修复数由后端数据构成。
- 空项目“上传第一篇论文”引导；已有解析失败项目可打开诊断。

**数据来源：** Project、Paper、Run、Capabilities。上传成功 202 后进入 review 路由，以 paper_id 查询进度；不能直接跳到有解析结果的页面。

原型的“最近项目得分”没有批量评分接口支持：首版列表展示状态、模式、时间、修复数及“查看报告”，**省略分数列**，避免前端逐行 N+1 读取报告或写假值。模型 chip 取 capabilities.models，首版只读，不做无接口的任意模型切换。

原型三个示例论文按钮不代表全部受支持。后端未提供真实案例资源前可隐藏；Mock 下可用清晰标识的场景切换器，不能在 live 下跳到假数据。

### 2. 论文解析与实验确认

路由 `/papers/:paperId/review`，用 `?experiment_id=...` 标明当前编辑实验。刷新先 GET Paper 取 project_id，再 GET 项目 experiments（paper_id 过滤），恢复已有实验；不能每次打开都创建新 Experiment。

左栏显示真实 PDF 与页码，右栏显示任务/数据/方法实体/公式和候选实验。点击证据跳页，有 bbox 才高亮；没有原文件时显示可读摘录与不可用提示。

**原型字段替换：**

- “解析置信度 92%”改为字段证据状态和需确认问题数量。
- 作者/venue/输入规格/硬件需求若当前 DTO 无字段，首版隐藏，不在前端猜数据；后续经契约变更再加。
- 数据和参数编辑来自 backend draft.editable_fields，不把原型可编辑样式当作真实允许范围。
- 一次选择一个候选实验，targets 来自该候选；首版不做任意跨表多选组合。

**表单行为：**

1. 选 candidate → 创建 Experiment → GET draft。
2. 用 draft.spec 完整初始化，基本字段只编辑允许路径，recipe_config 原样保留。
3. “保存并检查”提交新 revision，展示 blocking/warning/comparison_precheck/预算估计。
4. 无未保存修改、无 blocking issue、已显式接受 warning/费用时允许“确认并运行”。
5. confirm 和 runs 是两次独立请求。任一失败要保留已成功资源 ID，不能全流程重来。

对 smoke/scaled 明显提示“只验证流程/简化实验，不直接证明论文指标”。条件改变必须显示不可比原因。409 冲突保留当前内存编辑并提供加载最新版本，不自动覆盖。

### 3. 生成与执行工作台

路由 `/runs/:runId`。三栏保留文件树、代码、日志与修复时间线；窄屏可折叠。

- Run 快照决定状态、阶段、模式、修复次数、预算和当前候选。
- Attempt/files 决定文件树；点击文件实际 GET artifact text，代码 tab 不只是变色。
- patch_artifact_id 提供真实 diff；diagnosis_summary 为有证据的解释，不展示模型内部思维链。
- 日志是转义纯文本，支持暂停自动滚动/回到底部/下载完整日志；限定视图行数。
- 取消按钮只在活动状态且有权限时显示；点击 202 后为“停止确认中”，直到后端终态。
- awaiting_revision 显示“需重新确认实验”，导航回原实验，不直接重启旧 Run。
- completed 但报告仍 pending/generating 时显示“执行结束，报告生成中”。

六站流水线必须按真实状态派生，不按当前页面点亮：修复是条件分支，未发生时标“不需要”，不能强制每次都演一轮失败。queued 未开始运行；verifying 表示独立评测；报告站由 report_status 控制。所有失败都停在对应阶段并给出原因。

先完成 3 秒 polling 再接 SSE；事件只更新/失效 Query 与追加有界日志，快照是最终校准。刷新先补历史 attempts/log artifacts，不能只接从当前 seq 开始的 SSE 导致时间线空白。

### 4. 复现报告

路由 `/runs/:runId/report`。先显示 **实验模式与 verdict**，再显示辅助得分。

- 指标表：原文值、实测值、差异、容差和比较资格；null 显示“未测/不可比较”，不是 0。
- 总分 total=null 时不画 0/100 或绿色成功 badge。
- 曲线来自 curves endpoint；没数据显示空态，异单位/异横轴不强行叠加。
- 复现声明来自 manifest：seed、代码 hash、镜像、数据/split hash、环境和限制；没有 commit 就不造 commit。
- 导出按钮创建 Export，再轮询 items；已有 ready 文件直接下载。
- viewer 只下载已经生成的 ready 文件，没有“生成报告/包”的按钮。
- PDF 单项失败不遮挡已就绪 ZIP/JSON；显示失败格式与 request_id。

首版删除原型“公开分享”和“Docker 镜像”按钮；交付的是授权代码/日志/报告及镜像引用说明，不是已有镜像下载或公开社区。

## 🔄 状态、单位与错误展示规范

| Run.status | 中文文案 | GUI 重点 |
|---|---|---|
| queued | 排队中 | 不假报 GPU 正在训练 |
| generating | 生成代码中 | 只显示已生成文件 |
| executing | 正在执行 | 日志、资源、取消 |
| repairing | 自动修复中 | 错误及补丁证据 |
| verifying | 独立评测中 | 不提前展示成功分 |
| cancel_requested | 停止确认中 | 禁用重复取消 |
| completed | 执行完成 | 继续检查 report_status |
| failed | 执行失败 | 终止原因与诊断报告 |
| cancelled | 已停止 | 保留已产生证据 |
| awaiting_revision | 需要重新确认 | 回协议页创建新版本 |

verdict 文案：matched=选定指标在容差内；outside_tolerance=未达约定容差；not_comparable=条件不同，不可直接比较；execution_failed=未完成执行；insufficient_evidence=证据不足。不要使用“论文正确/错误”或“核心结论已被证明”。

金额 `amount_micros / 1_000_000` 转元，并区分实际、预留、未知；null 显示“未知”。ratio 才按需要乘 100 显示百分比；unit=percent 已经是百分数，不再乘 100；该单位的偏差显示 pp。所有差异、容差和 score 由后端计算。

时间显示本地时区，接口时间保留 UTC；elapsed 为 started_at 到 ended_at/当前时间的近似显示，不作为计费依据。取消一个任务不代表云主机已经停止收费。

Mock/live/replay 标识放在所有核心页头部：mock=测试数据；live=真实运行；replay=历史回放。真实 API 不可用时显示错误，不自动切假数据冒充恢复。

## 🧪 Mock 先行与联调验收

### Mock 场景清单

Mock 由前端维护，但字段必须符合共享 DTO；全部使用同一套关联 ID（project/paper/experiment/revision/run/attempt/artifact），不要每个页面造一套孤立对象。

| 场景 | 必须看到的结果 |
|---|---|
| M01 空项目 | 空态及上传入口，无假历史 |
| M02 上传/解析中 | 上传202、真实轮询结构、缺进度时不显示假百分比 |
| M03 解析失败/不支持 | 错误与支持范围，不进入执行页假装成功 |
| M04 缺参数/冲突 | blocking 禁确认，409 保留本地修改 |
| M05 排队至执行 | 按快照状态切换，不按定时动画自动成功 |
| M06 一次修复 | 错误、patch、候选版本和新日志对应 |
| M07 修复耗尽/预算停止 | 终态及诊断，不无限重试 |
| M08 取消竞态 | cancel_requested 到实际终态，完成也可胜出 |
| M09 断 SSE/401 | polling降级或登录，不丢主状态 |
| M10 完成但报告延迟 | 继续等待报告，不显示空白或虚假分 |
| M11 不可比结果 | total=null、无差异值、清楚模式标签 |
| M12 viewer/PDF部分失败 | viewer可下载现成文件，PDF失败不挡ZIP |

有界的 Mock 状态模拟可以使用计时器，但只在 mock-ui 且有测试标签；live 组件不能引用该计时器推动业务状态。

### 开发与联调批次

**F0 契约检查：** 前后端确认本文 v0.2；后端回答字段/状态问题，先修文档再开不同实现。队友不必等待完整后端才开工。

**F1 GUI Mock：** 前端搭四视图、路由、API adapter、M01–M12；展示所有状态，录制一次完整 Mock 路径并明确标注。

**F2 读接口联调：** 后端先给登录、capabilities、项目、Paper、Extraction、Run、Report 的真实查询/fixture接口。前端可先验证形状与权限，但此阶段不称真实复现完成。

**F3 写链路联调：** 接上传→draft→save→confirm→run→cancel→export，逐一验证幂等、409、422、202。默认先 CPU smoke，不以 GUI 测试自动授权云消费。

**F4 实时及验收：** 接 SSE、断线/刷新恢复、报告延迟和导出部分失败；最后用真实后端执行一次新上传闭环，由后端提供 run_id/证据。

### 前端向后端交付什么

- 完整 `apps/web` 源码、依赖与锁文件变更说明。
- 实际可用的启动/构建/测试命令及环境变量示例，不能只给截图。
- Mock/live 切换方式、所消费契约版本、页面路由和未完成项。
- M01–M12 测试结果和桌面/窄屏截图；失败或未验项目单列。
- 接口问题带 method/path、脱敏请求、response code、request_id、页面及复现步骤，不发送 cookie/邀请 token。

### 前端完成标准

- [ ] 四视图可刷新、可深链接、可回到已有项目/实验；没有点击导航就假完成流水线。
- [ ] 所有按钮有真实动作/状态反馈或明确禁用原因，没有空按钮。
- [ ] DTO 与 v0.2 一致；Mock 和 live 使用同一个 api-client。
- [ ] 未登录、viewer、401/403/404/409/422/429/503 都有合理界面。
- [ ] 日志/公式/Markdown/代码不可执行不可信 HTML；没有客户端模型 Key。
- [ ] null、百分数、百分点、金额 micros 和未知成本展示正确。
- [ ] SSE 断开、取消竞态、报告延迟、PDF 部分失败均可恢复/解释。
- [ ] 1440/1280 桌面可用，手机报告可读，键盘可操作，减少动画有效。
- [ ] 保留原型设计语言，但没有硬编码 87 分、92% 置信度、¥1.8 或假曲线作为真实数据。

## 📦 可以直接复制给队友的消息

> 我负责 Paper2Run 后端，你负责前端 GUI。先按《前后端公共接口契约 v0.2》和原型 HTML 做 React/TypeScript/Vite 前端，优先完成四视图和全部状态的 Mock，再接我的真实 API。你不用做 Qwen、数据库、Docker 或指标计算，也不要在前端补造这些结果。你的交付范围包含前端工程配置、路由、组件、API client、Mock 和前端测试；共享 API 类型和根锁文件变更需要我们串行同步。原型数据全是占位，不能当真实结果。重点做好上传、协议保存确认、执行日志/修复、取消、不可比较报告和下载；接口问题直接带 request_id 找我。

**本次交接只在本地生成资料，没有自动发送给队友、创建远程仓库或部署任何服务。**
