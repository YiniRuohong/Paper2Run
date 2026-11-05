# Paper2Run 文档索引

本目录只放文档；软件代码在仓库根的 `apps/`、`services/`、`packages/`、`scripts/`、`tests/`、`tools/`。

| 目录 | 内容 | 性质 |
| --- | --- | --- |
| [architecture.md](architecture.md) | 当前实现的软件架构、分层规则、主流程 | **现行**，随代码更新 |
| [components/](components/) | 组件说明：[执行目标（Docker）](components/sandbox.md) | **现行** |
| [engineering/](engineering/) | 00–13 号工程规划、接口契约、部署与任务包；[改进工作文档](engineering/12-Paper2Run自主实验框架改进工作文档.md)、[实施与验收](engineering/13-Paper2Run自主实验框架实施与验收.md) | 12 为已确认方向，13 记录实施及验收；历史固定配方限制不适用于自主模式，未验证能力不能视为完成 |
| [product/](product/) | PRD、静态交互原型、调研与竞品报告 | 原始需求依据 |
| [design/](design/) | 各阶段设计 spec 与实施 plan | 历史记录，路径以当时为准 |
| [verification/](verification/) | 各阶段验证记录 | 历史记录，路径以当时为准 |
| [handoff/](handoff/) | 阶段交接说明 | 历史记录 |
| [delivery/](delivery/) | 对外交付包（前端交接包 v0.2） | 快照 |

历史记录类文档（design/verification/handoff）保留写作时的代码路径。2026-11 起 `services/runner`、`services/kernel` 已下沉为 `packages/runner`、`packages/kernel`，`p2r_runner_service` 改名为 `p2r_orchestration.controlled_runner`。2026-11-05 起旧的固定配方 / 引导模式已删除：`packages/runner`、`packages/codegen`、`packages/kernel`、`p2r_orchestration` 中的旧流程模块、旧进程沙箱、`tools/benchmark` 以及 demo、sessions、experiments、runs、reports 等路由都已不在代码中，历史文档里的这些路径只能在 git 历史中查看。
