# Paper2Run 后续开发交接文档

更新时间：2026-09-30
工作区：`/Users/andyzhao/Documents/claude code/Ali_Hackthon/.worktrees/phase0-phase1`
分支：`feat/paper2run-phase0-phase1`

> **2026-09-30 晚间状态更新**：本文档第 5 节的 P0（真实 CPU Recipe）、P1（测试绿化、真实 Provider transport）、P2（Guided UI 真实入口）已完成，全量回归 `pytest -q` 527/0、前端 100/0、build 通过。详见 `docs/verification/2026-09-30-paper2run-real-closed-loop.md`。剩余方向：真实 SSH/远程执行（本文第 7 节前置清单）、真实 Provider 联网 E2E、GPU 与生产化基础设施。本文其余内容为当时的任务定义，保留作背景。


## 1. 交接目标

当前已经完成 Paper2Run 的产品流程 Demo 和主要安全边界。下一位开发者需要继续把 controlled smoke Demo 推进为一个可以真实运行至少一个本机 CPU 实验的 MVP。

目标不是一次性接入所有模型、GPU、SSH 和生产基础设施，而是优先打通一条真实、可重复、可审计的最小实验链路：

```text
真实文章/文本输入
→ 真实 Provider 结构化抽取
→ 用户审核 ExperimentSpec
→ EnvironmentCheck
→ 显式执行确认
→ 真实本机 CPU Recipe
→ 真实 predictions Artifact
→ 真实 Evaluation
→ 真实 Report
→ JSON/ZIP Export
```

当前 `offline_fixture/1` 只能作为回归和教学路径，不能当作论文复现或真实实验结果。

## 2. 当前已完成范围

### 2.1 文章、抽取和方案契约

相关代码：

- `packages/orchestration/p2r_orchestration/article_schema.py`
- `packages/orchestration/p2r_orchestration/extraction_service.py`
- `packages/orchestration/p2r_orchestration/providers.py`
- `packages/orchestration/p2r_orchestration/provider_sessions.py`
- `services/api/p2r_api/routes/papers.py`
- `services/api/p2r_api/routes/provider_sessions.py`
- `services/api/p2r_api/routes/experiments.py`

已具备：

- `EvidenceStatus`：`supported | inferred | missing | conflicting`；
- `support_status`：`supported | review_required | unsupported`；
- Pydantic schema 使用 `frozen=True` 和 `extra="forbid"`；
- `SourceRef.pdf_sha256` 校验和小写规范化；
- `execution_eligible()`；
- PDF/TXT/JSON/multipart 文章入口；
- SHA-256、UTF-8、来源引用归属校验；
- fake Provider transport；
- Provider response 的严格 JSON/schema 校验；
- Provider secret runtime-only 保存，不进入数据库、Artifact、日志、SSE、报告或导出；
- API 写入使用 `Idempotency-Key`；
- unknown write result 要求先查 receipt，再使用原 key/body 显式重放。

### 2.2 EnvironmentCheck

相关代码：

- `packages/orchestration/p2r_orchestration/environment_checker.py`
- `services/api/p2r_api/routes/environment.py`
- `packages/persistence/p2r_persistence/models.py`
- `packages/persistence/alembic/versions/0015_article_provider_environment.py`

已具备：

- 检查项状态：`pass | fail | unknown | not_needed`；
- 决策：`ready | blocked | unsupported`；
- `unknown` 不视为通过；
- 检查器只读，不执行修复命令；
- EnvironmentCheck 持久化和 GET/list API；
- 创建 Run 前要求关联 revision 的最新检查并满足执行门禁；
- fake environment probe。

仍然只是初步检查器，尚未覆盖完整 GPU/CUDA、依赖锁、数据集 SHA-256、模型权重和资源等级模型。

### 2.3 SSH 验证边界

相关代码：

- `packages/orchestration/p2r_orchestration/ssh_executor.py`
- `services/api/p2r_api/routes/environment.py`
- `tests/unit/phase02/test_ssh_executor.py`
- `tests/integration/phase02/test_ssh_environment_routes.py`

已具备：

- `SSHTarget` 结构化校验 host/port/user/workdir；
- 认证只允许受控 key reference 或 SSH agent id；
- 拒绝 password/private key/passphrase/secret/token；
- 拒绝 command/argv/shell/script；
- 只有 blocked EnvironmentCheck 且用户显式 `user_selected=true` 才能走 SSH；
- fake SSH verifier；
- 成功验证生成 `SSHVerificationRecord`；
- 成功状态为 `awaiting_confirmation`；
- 验证本身不创建 Run；
- 失败不落验证记录；
- GET readback；
- `SSHVerificationRecord.updated_at` 已设置 `onupdate`。

真实 SSH transport 按当前约束尚未实现。

### 2.4 Run、Report、Runner 真实性边界

相关代码：

- `services/api/p2r_api/routes/runs.py`
- `services/api/p2r_api/routes/reports.py`
- `services/runner/p2r_runner.py`
- `packages/persistence/p2r_persistence/models.py`
- `packages/persistence/alembic/versions/0016_runs_execution_truth.py`
- `tests/integration/phase02/test_execution_truth.py`

已具备：

- Run 创建前检查 registered recipe；
- candidate 必须显式 `execution_support="supported"`；
- blocked/missing EnvironmentCheck 拒绝本机 Run；
- SSH Run 必须绑定归属正确的 verification；
- SSH verification 必须为 `awaiting_confirmation`；
- SSH Run 必须 `execution.confirmed=true`；
- SSH Run 当前只记录已验证目标，不入队本机 fixture，不伪造远程结果；
- `RunRecord` 持久化 `environment_check_id` 和 `target_json`；
- Report 需要 completed Run 和 Evaluation；
- Report snapshot 包含 target、recipe、environment、source、limitations、artifact/evidence metadata；
- Runner registry 当前只允许 `offline_fixture/1`；
- Runner 拒绝未知 recipe、非 local target、任意 parameters、shell、argv、任意 workdir；
- fixture output 标记 `evidence_class=controlled_smoke`。

## 3. 当前前端状态

相关代码：

- `apps/web/src/features/guided/GuidedPage.tsx`
- `apps/web/src/features/guided/controller.ts`
- `apps/web/src/features/guided/state.ts`
- `apps/web/src/features/guided/readback.ts`
- `apps/web/src/features/guided/guided.css`
- `apps/web/src/api-client/guided.ts`
- `apps/web/src/api-client/guided-types.ts`

当前 Guided 页面可以展示 controlled smoke 的单列流程：

```text
Paper → Extraction → Experiment → Run → Report → Export
```

已增加：

- Run 状态的诚实中文说明；
- SSH 无远程执行适配器的明确提示；
- Run 非 completed 时禁止构建 Report；
- GET-only refresh/readback；
- 不把 secret 写入 localStorage。

但页面目前的主要入口仍是“离线教学示例”，真实 Provider 表单、真实文章输入、多候选 ExperimentSpec、EnvironmentCheck 详情和 SSH 交互仍未完整接入产品 UI。

## 4. 已验证结果

最近已实际验证：

```text
PYTHONPATH=. .venv/bin/python -m pytest \
  tests/integration/phase02/test_execution_truth.py \
  tests/integration/phase02/test_experiments_runs_http.py \
  tests/unit/phase02/test_ssh_executor.py \
  tests/integration/phase02/test_ssh_environment_routes.py -q
```

结果：

```text
28 passed
```

```text
PYTHONPATH=. .venv/bin/python -m pytest \
  tests/unit/phase02 \
  tests/integration/phase02/test_guided_bootstrap.py \
  tests/integration/phase02/test_guided_schema.py -q
```

结果：

```text
213 passed
```

前端：

```text
npm test --prefix apps/web
67 passed, 0 failed
```

```text
npm run build --prefix apps/web
成功
```

```text
git diff --check
通过
```

完整后端测试当前仍不是全绿：

```text
pytest -q
471 passed, 21 failed
```

21 个失败集中在旧的 export/guided-export 测试 harness 没有显式创建 EnvironmentCheck，调用旧 `_prepare_run` 后被正确的 Run 门禁拒绝。下一位开发者应修复这些测试 harness 的前置条件，而不是放宽产品门禁。

## 5. 最优先剩余任务

### P0：真实本机 CPU Recipe

这是当前距离“真实实际闭环产品”最近、价值最高的任务。

建议新增一个固定、可重复、资源受控的 Recipe，例如：

```text
real_cpu_baseline / 1
```

要求：

1. 使用固定小型公开或仓库内测试数据；
2. 不依赖 GPU；
3. 不联网下载；
4. 依赖已在当前环境中存在，不能安装依赖；
5. Recipe 有明确参数 schema；
6. Recipe 参数不能来自任意 shell/argv；
7. Runner 根据后端注册 Recipe 生成受控执行计划；
8. 执行产生真实 predictions Artifact；
9. Evaluation 使用真实 predictions 计算指标；
10. Report 标明真实 Recipe、版本、target、environment snapshot、source refs 和 artifact metadata；
11. 失败、超时、取消都不能生成成功报告。

建议先不要做任意用户代码执行，也不要做通用 shell runner。

### P1：修复完整测试前置条件

重点检查：

- `tests/integration/phase02/test_export_artifacts_http.py`
- `tests/integration/phase02/test_guided_exports*`
- 其他使用旧 `_prepare_run` 的测试。

在测试 helper 中显式创建：

- supported candidate；
- SpecRevision；
- Confirmation；
- `EnvironmentCheckRecord(decision="ready")`；
- 必要时明确 local execution target。

不要通过删除或弱化 `ENVIRONMENT_CHECK_REQUIRED`、`UNSUPPORTED_RECIPE` 或 confirmation gate 来修复测试。

目标：

```text
PYTHONPATH=. .venv/bin/python -m pytest -q
```

最终达到全绿，或把确实不适用的历史测试明确拆分并记录原因。

### P1：接入真实 Provider transport

建议只先接一个 OpenAI-compatible Provider，使用注入的 async HTTP transport 结构保留测试可替换性。

需要补：

- 真实请求 headers/body；
- 超时；
- 错误分类；
- JSON/schema 响应解析；
- refusal/truncation/invalid JSON；
- 文章长度限制和分块策略；
- call id、token/cost metadata；
- 不记录 secret 和原始敏感 response。

不得把 Provider secret 放到前端 localStorage、数据库、Artifact、报告或日志。

### P2：完善真实 Article → Experiment UI

把现有 Guided 页面从 fixture-only 入口扩展为真实入口：

- 文件/粘贴文本二选一；
- Provider endpoint/model/API key 表单；
- API key 仅内存使用，刷新后重新输入；
- 抽取 loading/error/ready；
- Extraction source refs 和 evidence status；
- 候选 ExperimentSpec 选择；
- supported/review_required/unsupported 分支；
- EnvironmentCheck 项目详情；
- blocked 时显示“改用 SSH”和“只保存方案”；
- SSH target 只接受 host/port/user/key reference/agent id/workdir；
- SSH 未验证前隐藏执行确认按钮；
- Run/Report/Export readback。

## 6. 后续真实执行器设计约束

无论下一位开发者选择 subprocess、容器还是远端执行，都必须保持：

- Runner 只接受已注册 Recipe；
- 参数通过 Recipe schema 校验；
- 不接受模型生成的任意 shell、argv、workdir；
- 不接受用户提交的任意命令；
- 工作目录和输入输出 Artifact 隔离；
- stdout/stderr 有大小上限并脱敏；
- timeout、cancel、lease、fencing 可验证；
- 结果必须经过 Artifact hash 校验；
- 只有真实 completed Run 才能生成成功 Report；
- controlled smoke 和真实实验必须有不同 evidence class；
- 报告必须保留 limitations，不能把教学指标写成论文性能。

## 7. SSH 后续实现边界

当前 SSH 只能 fake verify。若后续实现真实 SSH，必须先完成：

1. known_hosts/host key 策略；
2. key reference/agent id 的安全解析；
3. 连接和命令超时；
4. 远端环境只读检查；
5. 远端 Recipe registry；
6. Artifact 上传/下载；
7. 远程日志和状态回传；
8. 取消与断线恢复；
9. verification 过期/撤销；
10. 用户确认后才允许远程执行。

在真实 SSH 适配器完成前，不能声称 SSH 闭环已经完成。

## 8. 不要做的事情

- 不要使用 `git reset`、`git checkout`、`git clean`；
- 不要覆盖或删除其他既有 modified/untracked 文件；
- 不要 commit 或 push，除非用户明确要求；
- 不要安装依赖；
- 不要联网部署；
- 不要调用真实模型、GPU、真实 SSH 或外部 Runner，除非用户单独授权并更新约束；
- 不要把 fake Provider/fake Environment/fake SSH 的测试结果写成真实能力；
- 不要用放宽门禁的方式修复旧测试；
- 不要为修复测试而让 unknown 环境检查变成 ready；
- 不要将密码、私钥正文、API key 或 token 写入代码、fixture、日志或文档；
- 不要引入第二套 Paper → Extraction → Experiment → Run → Report 持久化事实来源。

## 9. 推荐开发顺序

```text
1. 修复旧测试 harness 的 EnvironmentCheck 前置条件
2. 让 pytest -q 全绿
3. 设计并实现一个真实 CPU Recipe
4. 打通真实 predictions → Evaluation → Report → Export
5. 接入一个真实 Provider transport
6. 扩展 Guided UI 到真实文章/Provider/Environment 流程
7. 完成真实浏览器 E2E 和下载落盘验证
8. 最后实现真实 SSH/远程执行和生产化基础设施
```

如果时间有限，优先完成第 1–4 步。这会把项目从“流程演示 Demo”提升为“能真实跑一个实验的 MVP”。

## 10. 下一位 Agent 的首个任务包

建议直接把以下任务交给下一位实现 Agent：

> 在 `/Users/andyzhao/Documents/claude code/Ali_Hackthon/.worktrees/phase0-phase1` 中，先修复所有旧 export/guided-export 测试 helper，使其显式创建 supported candidate 和 ready EnvironmentCheck；不得放宽产品 Run 门禁，不得改成自动通过 unknown。运行 `PYTHONPATH=. .venv/bin/python -m pytest -q`，目标是消除当前 21 个旧失败。只修改测试 helper、测试 fixture 或确实必要的测试适配文件，不修改 Provider、SSH、Runner 真实性边界。不要安装依赖、不要 commit/push。完成后报告修改文件和完整测试结果。

完成测试绿化后，再单独派发真实 CPU Recipe 任务，不要把两个任务混在一个子任务中。

## 11. 代码与验证文档

主要验证记录：

- `docs/verification/2026-09-29-paper2run-phase0-phase1.md`
- `docs/handoff/2026-09-27-paper2run-handoff.md`
- `docs/design/plans/2026-09-29-paper2run-article-to-execution-plan.md`
- `docs/design/specs/2026-09-29-paper2run-article-to-execution-design.md`

当前交接文档：

- `docs/handoff/2026-09-30-paper2run-handoff.md`

## 12. 子代理委托注意事项

当前运行时对模型名要求 provider/model 格式。已验证可用的 GLM canonical key 是：

```text
custom_provider:glm/glm-5.3-flash
```

不要直接传：

```text
GLM/GLM-5.3-Flash
```

后续派遣时：

- 只读检索使用 `explore`；
- 已批准的闭合代码任务使用 `implementer`；
- 提示词必须自包含必要上下文；
- 明确允许修改文件、禁止路径、验收命令；
- 限定读取范围，避免子代理重复读取同一文件而卡住；
- 不要让多个写入型子代理并行修改同一文件；
- 不要让子代理自行扩大架构范围；
- 子代理结果必须由主 Agent 检查和验证后再向用户汇报。
