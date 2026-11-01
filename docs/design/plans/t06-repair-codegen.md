# T06 有限修复与受控代码生成最小闭环

## 目标
在不接入真实模型、不执行任意用户代码、不改依赖锁的前提下，实现可独立测试的确定性 codegen/repair 边界：文件 allowlist、路径与大小限制、结构化 patch、hash 锚定、repair ordinal/预算/重复错误停机、来源披露与审计载荷。

## 范围
- 新增 `packages/codegen/` 纯 Python 模块和其单元测试。
- 不改现有 HTTP、数据库迁移、Runner、评测器和前端。
- 仅提供供后续 Worker/API 接入的纯函数/数据结构，不假装已完成生产执行链。

## 必须行为
1. 代码候选只能包含 allowlist 文件；路径必须相对、POSIX、不可穿越、不能是目录；单文件和总大小受限。
2. 生成/修复候选需规范化后计算 manifest/hash；任何外部提供的 hash 不匹配拒绝。
3. patch 是结构化的 replace/add/delete 操作，操作文件必须在 allowlist；replace 必须匹配期望 base hash；delete/add 遵守限制。
4. 修复 ordinal 为 1..5；超过上限拒绝。每个 experiment 累积预算不因派生 run 重置。
5. 重复错误（同 error code + base code hash）停机；预算不足停机；需要改变实验语义时输出 `needs_revision`，不能生成技术 patch。
6. 来源必须区分 `template`、`model`、`repair`，并包含可审计的 base hash/patch hash/validation 结果；不记录 secret。

## 验证
- 先新增失败优先单元测试并确认失败。
- 实现后运行新增测试、phase02 测试、全量 pytest、compileall、git diff --check。
