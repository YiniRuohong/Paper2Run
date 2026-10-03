# Paper2Run Phase0/Phase1 验证记录（2026-09-29）

## 本轮范围

文章输入、结构化抽取、Provider session、EnvironmentCheck、手动 SSH 验证、受控 Run/Report/Runner 边界，以及 Guided 单列 Demo 的状态展示。

约束：未调用真实 Provider、GPU、SSH、外部 Runner；未执行下载落盘验收；controlled smoke 不代表论文复现。

## 已验证

- SSH 与环境边界定向测试：`28 passed`。
- Phase02 unit + Guided bootstrap/schema：`213 passed`。
- Web Node 测试：`67 passed, 0 failed`。
- Web production build：成功（TypeScript 检查和 Vite build 均通过）。
- `git diff --check`：通过。
- Task6 execution truth 定向测试：`12 passed`（包含 unsupported/blocked/未确认 target/SSH verification/report gate/runner registry）。

## 真实性边界

- Runner 仅允许注册的 `offline_fixture/1`，输出标记 `evidence_class=controlled_smoke`。
- 本机 Run 固定受控 local-cpu target；SSH verification 成功只进入 `awaiting_confirmation`，SSH Run 当前只记录已验证目标，不冒充已远程执行。
- 报告构建要求 completed Run 和 evaluation；报告快照包含 target、recipe、environment、source、limitations 和 artifact/evidence metadata。
- Provider secret 不进入浏览器持久化、数据库、Artifact、SSE、报告或导出。

## 已知未通过/未执行

直接运行完整 `pytest -q` 仍有 21 个失败，集中在既有 export/guided-export harness 未显式准备 EnvironmentCheck；这些测试调用旧 `_prepare_run`，属于测试前置条件迁移问题，不应通过放宽产品门禁修复。本轮未修改这些历史测试 harness。

Alembic 0016 已由 schema 测试路径验证；未对外部持久化数据库手工执行 upgrade/downgrade。真实 SSH、真实 Provider、GPU/外部 Runner 和下载落盘均按约束跳过。
