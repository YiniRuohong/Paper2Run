# 自主模式后端：候选关卡、独立复核与结局 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在自主模式的 Python 控制面中实现以下机制：`submit_candidate` 证据关卡、冻结快照、在新会话中独立复核、自动修复轮次、四种结局、只陈述事实的续写，以及按轮次存储的证据。Pi 推进科研的方式不受限制。

**Architecture:** 所有工具调用都要经过 `worker.dispatch()`，可信判断全部放在 Python 层。主会话每结束一轮，就交给新的 `driver.after_turn()` 决定下一步：续写、复核，或者结束任务。复核会另开一个 `HarnessBridge` 会话，这个会话只能访问宿主侧的冻结副本。Node harness 不做改动。

**Tech Stack:** Python 3.11+、SQLAlchemy、Alembic、pytest；Node Pi harness（`apps/harness/bridge.mjs`），本计划不修改它。

**Spec:** `docs/superpowers/specs/2026-11-05-autonomous-only-research-stages-design.md`（本计划对应 5.1–5.9 节，并按 Task 7 修订 5.5 节）。前端、删除传统模式、Windows 移植各自另写计划。

**工作目录：** 仓库根目录 `/Users/andyzhao/Documents/claude code/Ali_Hackthon`，分支 `feat/autonomous-only`。测试命令：`.venv/bin/python -m pytest …`。

## Global Constraints

- 分层规则：只能从更低的层导入。`p2r_orchestration` 是 L3，`p2r_contracts` 是 L0，`p2r_persistence` 和 `p2r_evaluation` 是 L1。`tests/unit/test_architecture.py` 和 `tests/unit/phase01/test_packaging.py` 必须始终通过。
- 证据类别和结局只能由程序写入。模型的叙述放进报告的 `model_narrative` 字段，不能用来确立结论；`verdict` 始终保持为 `not_comparable`。
- `autonomous_events` 只能追加。凭据不得出现在事件、产物或导出中（用现有的 `service.profiles.clean`）。
- 应用退出或任务暂停时，不能终止后台作业（`target.close()` 的语义保持不变）。
- 修复轮次上限：`limits.max_repair_rounds`，默认值 `3`，取值范围 `0..10`。
- 续写时，连续 `3` 次没有进展就结束为 `incomplete`。
- 复核失败只重试 `1` 次。
- 每个 Task 完成后，`tests/unit/autonomous` 必须全部通过，再提交。提交使用 conventional commits，提交信息末尾加上 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`。

## Review Focus

1. **Pi 提交候选后继续调用写入类工具**：此时应拒绝 `register_artifact` 和 `submit_candidate`，返回 `CANDIDATE_UNDER_REVIEW`，并保证快照不变。测试放在 Task 3：`test_register_refused_while_candidate_under_review`。
2. **复核期间应用退出，之后再恢复**：恢复后要针对同一份快照重新复核，不能重复增加轮次，也不能重新冻结快照。测试放在 Task 5：`test_resume_with_pending_review_reruns_same_snapshot`。
3. **复核者传入 `../` 或绝对路径试图读出冻结目录**：应当拒绝。测试放在 Task 4：`test_frozen_target_rejects_escape`。
4. **`max_repair_rounds=0`**：第一次复核结果是 issues 时，直接给出 `completed_with_issues`。测试放在 Task 5：`test_zero_repair_rounds_finishes_on_first_issues`。
5. **旧版自主任务（`harness_json` 为空）调用恢复接口**：返回 409 `LEGACY_TASK`，任务状态不变。测试放在 Task 6：`test_legacy_task_cannot_resume`。

---

### Task 1: 契约与持久化字段

**Files:**
- Modify: `packages/contracts/p2r_contracts/autonomous.py`（`Limits`，约第 78 行）
- Modify: `packages/persistence/p2r_persistence/autonomous.py`（`AutonomousTaskRecord`）
- Modify: `packages/persistence/p2r_persistence/db.py:66-91`（把 `_ensure_reference_columns` 扩展为同时补齐新列；函数名改为 `_ensure_autonomous_columns`）
- Create: `packages/persistence/alembic/versions/0022_autonomous_harness.py`
- Modify: `packages/orchestration/p2r_orchestration/autonomous/service.py`（`create_task`、`public`）
- Test: `tests/unit/autonomous/test_contracts.py`、`tests/unit/autonomous/test_harness_db_upgrade.py`（新建，结构仿照 `test_reference_db_upgrade.py`）、`tests/unit/autonomous/test_service.py`

**Interfaces:**
- Produces:
  - `Limits.max_repair_rounds: int = Field(default=3, ge=0, le=10)`
  - `AutonomousTaskRecord.outcome: Mapped[str | None]`，类型 `String(32)`，可空
  - `AutonomousTaskRecord.harness_json: Mapped[dict | None]`，类型 JSON，可空。新任务的初始值是 `{"version": 2, "repair_round": 0, "no_progress": 0, "fingerprint": None, "candidate": None, "reviews": []}`。`harness_json` 为空就表示这是旧版任务。
  - `service.public(row)` 的返回值新增 `outcome`、`repair_round`，以及 `candidate`（即 `harness_json["candidate"]`，旧任务为 `None`）和 `reviews`。
  - `OUTCOMES = ("verified", "completed_with_issues", "review_unavailable", "blocked_by_safety")`，定义在 `p2r_contracts/autonomous.py`。

- [ ] **Step 1: 写失败测试**
  - `test_limits_default_repair_rounds`：`Limits(...)` 不传这个字段时，`max_repair_rounds == 3`；传 `11` 或 `-1` 时抛出 `ValidationError`。
  - `test_new_task_has_harness_state`：调用 `create_task` 之后，`row.harness_json == {"version": 2, "repair_round": 0, "no_progress": 0, "fingerprint": None, "candidate": None, "reviews": []}`，并且 `row.outcome is None`。
  - `test_harness_columns_added_in_either_order`：仿照 `test_explicit_migration_and_startup_in_either_order`，覆盖两种顺序：先迁移到 0021 再执行 `init_db`，以及先执行 `init_db` 再迁移到 0022。两种顺序跑完都必须有这两列，旧行的 `harness_json` 都为空。
- [ ] **Step 2: 运行测试，确认失败**：`.venv/bin/python -m pytest tests/unit/autonomous/test_contracts.py tests/unit/autonomous/test_harness_db_upgrade.py tests/unit/autonomous/test_service.py -q`
- [ ] **Step 3: 实现。** 迁移 0022 按 0021 的写法逐列检查是否已存在（`down_revision = "0021"`）；`init_db` 的补列助手要覆盖 0021 和 0022 的全部列；`create_task` 写入初始 `harness_json`；`public()` 补上新增字段。
- [ ] **Step 4: 运行 `tests/unit/autonomous` 和两个不变量测试，确认全部通过**
- [ ] **Step 5: 提交** `feat(autonomous): persist harness state, outcome and repair round limit`

### Task 2: 按轮次存储证据

**Files:**
- Modify: `packages/orchestration/p2r_orchestration/autonomous/evidence.py`（`artifact_root`、`register`、`verified_files`、`evaluation`、`report`）
- Modify: `packages/orchestration/p2r_orchestration/autonomous/tools.py`（`register_artifact` 的 kind 枚举新增 `analysis`，同时更新描述中关于不可变性的说法）
- Test: `tests/unit/autonomous/test_service.py`

**Interfaces:**
- Consumes: Task 1 中的 `row.harness_json["repair_round"]`
- Produces:
  - `current_round(row) -> int`：`harness_json` 为空时返回 `0`
  - `artifact_root(service, row, round_=None) -> Path`：路径为 `task_root/artifacts/<protocol_sha|unversioned>/r<round>`。旧任务（`harness_json` 为空）仍使用原路径 `task_root/artifacts/<protocol_sha|unversioned>`。
  - 产物记录新增两个字段：`"round": int`，以及 `"sequence": int`（即写入 `artifact_frozen` 事件后的 `row.event_sequence`）
  - `round_artifacts(row) -> list[dict]`：返回当前协议版本、当前轮次的产物

- [ ] **Step 1: 写失败测试**
  - `test_same_name_allowed_in_new_round`：在第 0 轮登记 `main.py`，把 `repair_round` 改为 1 后再登记 `main.py`，两次都成功，文件分别位于 `r0/main.py` 和 `r1/main.py`。
  - `test_same_name_refused_within_round`：同一轮次内重复登记同名产物，抛出 `ARTIFACT_IMMUTABLE`。
  - `test_artifact_limit_counts_per_round`：在第 0 轮登记满 256 个后，第 1 轮仍可以继续登记。
  - `test_verified_files_reads_legacy_layout`：如果 `harness_json` 为空、产物记录里没有 `round`，`verified_files` 从原路径读取，并能通过哈希校验。
  - `test_report_uses_current_round_only`：`report()["artifacts"]` 只包含当前轮次的产物。
- [ ] **Step 2: 运行测试，确认失败**
- [ ] **Step 3: 实现。** `verified_files` 按每条产物记录自带的 `round` 拼出路径，没有 `round` 字段的走旧路径；导出时 zip 内的路径同样带上 `r<round>`。
- [ ] **Step 4: 运行 `tests/unit/autonomous`，确认通过**
- [ ] **Step 5: 提交** `feat(autonomous): key frozen evidence by repair round`

### Task 3: `submit_candidate` 关卡、冻结快照、工具与提示词

**Files:**
- Modify: `packages/orchestration/p2r_orchestration/autonomous/evidence.py`：把 `report()` 中解析入口执行的代码（shlex 判定 `matches_protocol_entrypoint` 那一段）抽成 `entrypoint_executions(db, row) -> list[dict]`；`report()` 改为调用它。
- Create: `packages/orchestration/p2r_orchestration/autonomous/candidate.py`
- Modify: `packages/orchestration/p2r_orchestration/autonomous/tools.py`：在 `TOOLS` 中删除 `evaluate` 和 `report`，新增 `note_progress` 和 `submit_candidate`，并重写 `SYSTEM_PROMPT`
- Modify: `packages/orchestration/p2r_orchestration/autonomous/worker.py`（`dispatch`）
- Test: `tests/unit/autonomous/test_candidate.py`（新建）；同时修改 `tests/unit/autonomous/test_service.py:187` 等依赖 `evaluate`/`report` 工具的旧测试，改为直接调用 `evidence.evaluation` / `evidence.report`

**Interfaces:**
- Consumes: Task 2 中的 `round_artifacts`、`artifact_root`、产物记录的 `sequence`
- Produces（都在 `candidate.py` 中）：
  - `gate(db, row) -> list[str]`：返回缺失项，空列表表示通过。依次检查三项，对应的缺失项写法固定为：`"frozen protocol"`、`"<kind> artifact"`（kind 依次为 code/environment/data/result，缺哪个写哪个）、`"successful entrypoint run after latest code registration"`。第三项的判定规则：在 `entrypoint_executions` 的结果中，存在 `matches_protocol_entrypoint` 为真、`exit_code == 0`，并且 `sequence` 大于当前轮次最后一个 code 产物的 `sequence` 的记录。
  - `freeze(service, target, task_id, summary: str) -> dict`：把当前轮次的产物复制到 `task_root/candidates/<snapshot_sha256>/artifacts/`，再用 `target.logs(job_id, 0, 1_000_000)` 拉取正式运行作业的日志，写入 `.../logs/<job_id>.txt`。之后写入 `harness_json["candidate"] = {"snapshot_sha256", "round", "protocol_sha256", "artifacts": [...], "run": {"job_id", "exit_code", "sequence"}, "summary", "review_status": "pending", "attempts": 0}`，并记录 `candidate_frozen` 事件。`snapshot_sha256` 用 `security.digest` 对以下内容计算：`{protocol_sha256, round, artifacts(name/kind/sha256), run}`。
  - `CANDIDATE_UNDER_REVIEW`：当 `candidate.review_status == "pending"` 时，调用 `register_artifact` 或 `submit_candidate` 都会被拒绝（409）。
- `note_progress(phase: str[1..120], summary: str[1..2000])`：只写一条 `progress_noted` 事件，返回 `{"recorded": true}`。
- `submit_candidate(summary: str[1..16000])`：关卡不通过时返回 409 `CANDIDATE_INCOMPLETE`，错误信息中列出缺失项；通过时返回 `{"status": "candidate_submitted", "snapshot_sha256": …, "round": …, "next": "End this turn. Evaluation and an independent review run after the turn; findings, if any, arrive in the next message."}`。
- `SYSTEM_PROMPT` 必须写明以下内容，并且**不能给出步骤顺序**：可用工具；`submit_candidate` 的三项证据要求；复核由另一个会话完成，问题会在下一条消息中送达；最多可修复 `max_repair_rounds` 轮；四种结局的含义；保留现有提示词中的这些规则：直接用解释器启动入口、不得编造执行结果、凭据不能作为实验输入、协议变更需要提交 proposal。

- [ ] **Step 1: 写失败测试**（`test_candidate.py`）
  - `test_gate_lists_missing_items`：一份空任务调用 `gate`，返回值等于 `["frozen protocol", "code artifact", "environment artifact", "data artifact", "result artifact", "successful entrypoint run after latest code registration"]`。
  - `test_gate_requires_run_after_latest_code`：先运行成功、之后才登记 code，此时仍缺第三项；在登记之后再运行成功一次，`gate` 返回 `[]`。
  - `test_submit_candidate_freezes_copies`：调用成功后，冻结目录中有四类产物和日志文件，哈希与原产物一致，`review_status == "pending"`，并产生一条 `candidate_frozen` 事件。
  - `test_register_refused_while_candidate_under_review`：审核期间调用 `register_artifact`，返回 `CANDIDATE_UNDER_REVIEW`，`candidate` 保持不变。
  - `test_evaluate_and_report_not_exposed`：`{t["name"] for t in TOOLS}` 中没有 `evaluate` 和 `report`，但包含 `note_progress` 和 `submit_candidate`。
  - `test_note_progress_records_event`。
  - `test_prompt_has_no_step_order`：`SYSTEM_PROMPT` 中包含 `submit_candidate` 和 `max_repair_rounds` 这两个字符串，并且不包含 `"Step 1"`、`"first,"`、`"then "` 这几种表示步骤顺序的写法。
- [ ] **Step 2: 运行测试，确认失败**
- [ ] **Step 3: 实现。** `dispatch` 中的 `submit_candidate` 按 `register_artifact` 的方式处理：先在加锁状态下读取并检查，然后在锁外调用 target，最后加锁写回。
- [ ] **Step 4: 运行 `tests/unit/autonomous`，确认通过**（失败的旧测试按上面 Files 中说明的方式修改）
- [ ] **Step 5: 提交** `feat(autonomous): gate results on submit_candidate with frozen snapshot`

### Task 4: 复核会话

**Files:**
- Create: `packages/orchestration/p2r_orchestration/autonomous/review.py`
- Test: `tests/unit/autonomous/test_review.py`（新建）

**Interfaces:**
- Consumes: Task 3 中的 `harness_json["candidate"]` 和冻结目录
- Produces:
  - `class FrozenTarget(root: Path, candidate: dict)`：提供 `read(path: str, offset: int = 0, limit: int = 16000) -> dict`、`logs(job_id: str, offset=0, limit=16000) -> dict`、`status(job_id: str) -> dict`。`read` 只能读取 `root/artifacts` 下的文件，`path` 可以写产物名，也可以写产物的 `source_path`；路径中含 `..`、是绝对路径、或者是符号链接时，返回 `INVALID_PATH`。`logs` 和 `status` 只接受 `candidate["run"]["job_id"]`。
  - `REVIEW_TOOLS`：包含 `read`、`logs`、`status`、`submit_review`。`submit_review` 的参数：`verdict` 取值为 `pass` 或 `issues`；`findings` 是数组，最多 50 条，每条是 `{"description": str[1..4000], "evidence": [str]}`。
  - `REVIEW_PROMPT`：要求复核者对照冻结的协议和论文上下文，检查实现、数据、运行和结果是否一致，只能根据读到的证据下结论；结论为 `issues` 时，必须至少给出一条带证据引用的 finding。
  - `run_review(service, task_id, bridge, model: dict, account: Callable[[dict], None]) -> dict`：建立一个新会话（`session_dir = task_root/review/<snapshot_sha256>/attempt-<n>`，不带 specialists，`limits` 取 `service.remaining(row)`），把论文上下文、协议、快照清单和评测结果拼成一段 JSON 作为输入。会话中的 `event`/`result` 消息交给 `account` 处理，用于计费；`tool_request` 交给 `FrozenTarget` 或 `submit_review` 处理。正常情况下返回 `{"verdict", "findings"}`。如果会话结束时还没有提交 `submit_review`，或者参数不合法，或者遇到 error 消息，就抛出 `ReviewFailed(reason: str)`。
  - 评测在调用 `run_review` 之前执行，用的是 `evidence.evaluation(service, row)`，评测对象是冻结目录（`evaluate_artifacts(protocol_json, root/artifacts)`）。实现时在 `evidence.py` 中新增 `evaluation_at(service, row, root) -> dict`，`evaluation()` 改为调用它。

- [ ] **Step 1: 写失败测试**（用测试替身 Bridge 按脚本发送 `tool_request`）
  - `test_frozen_target_rejects_escape`：`read("../x")`、`read("/etc/passwd")`，以及读取指向目录外的符号链接，都返回 `INVALID_PATH`。
  - `test_frozen_target_ignores_live_changes`：冻结之后修改 Target 中的文件，复核者 `read` 到的仍是冻结时的内容。
  - `test_logs_only_for_candidate_job`：用其他 job_id 调用，返回 `JOB_NOT_OWNED`。
  - `test_run_review_returns_verdict`：脚本依次执行 read → submit_review(pass)，返回 `{"verdict": "pass", "findings": []}`。
  - `test_issues_requires_finding`：`submit_review(issues, [])` 返回工具错误；如果会话随后结束，抛出 `ReviewFailed`。
  - `test_review_has_no_mutating_tools`：`{t["name"] for t in REVIEW_TOOLS} == {"read", "logs", "status", "submit_review"}`。
- [ ] **Step 2: 运行测试，确认失败**
- [ ] **Step 3: 实现**
- [ ] **Step 4: 运行 `tests/unit/autonomous`，确认通过**
- [ ] **Step 5: 提交** `feat(autonomous): independent review session over frozen candidate`

### Task 5: 轮次驱动、续写与结局

**Files:**
- Create: `packages/orchestration/p2r_orchestration/autonomous/driver.py`
- Modify: `packages/orchestration/p2r_orchestration/autonomous/worker.py`：`result` 分支不再自动生成报告，改为只写一条 `turn_ended` 事件；`bridge.run(...)` 改为在一个循环中调用；`finally` 中预算耗尽的处理改为根据结局判断
- Modify: `packages/orchestration/p2r_orchestration/autonomous/evidence.py`：`report()` 新增参数 `outcome: str | None`，报告中写入 `outcome`、`candidate`、`reviews`
- Test: `tests/unit/autonomous/test_driver.py`（新建）；同时修改 `test_service.py`、`test_acceptance.py`、`test_worker_terminal_race.py`、`tests/integration/autonomous/test_api.py` 中断言"会话一结束就 completed"的旧用例

**Interfaces:**
- Consumes: Task 3 的 `candidate`，Task 4 的 `run_review`、`ReviewFailed`、`evaluation_at`
- Produces（都在 `driver.py` 中）：
  - `fingerprint(row) -> list`：由 `[len(row.artifacts_json), 已结束作业的数量, 当前候选的 snapshot_sha256 或 None]` 组成。
  - `facts_message(row) -> str`：续写消息。只陈述事实：已登记的产物（名称、kind、轮次）、作业状态、`gate()` 列出的缺失项、剩余预算、剩余修复轮次、最近一次复核的 findings（如果有）。消息中不能出现建议下一步做什么的语句。
  - `after_turn(service, task_id, bridge_factory, model, account) -> tuple[str, str | None]`：返回 `("continue", prompt)` 或 `("finished", None)`，规则如下：
    1. 有 `review_status == "pending"` 的候选时：先执行评测，再调用 `run_review`。失败时 `attempts + 1`，`attempts` 达到 2 后结局为 `review_unavailable`。
       - 结果为 `pass`：`finish(..., "verified")`。
       - 结果为 `issues`，并且 `repair_round < max_repair_rounds`：`repair_round + 1`，把候选的 `review_status` 改为 `"issues"`，把这次复核结果追加到 `reviews`，然后返回 `continue`，消息内容是 findings 原文加上 `facts_message`。
       - 结果为 `issues`，但轮次已用完：`finish(..., "completed_with_issues")`。
       - 每次复核都写一条 `review_recorded` 事件，内容包括 `snapshot_sha256`、`verdict`、`findings`、`attempt`。
    2. 没有待复核的候选时：比较 `fingerprint`。与上次相同则 `no_progress + 1`，否则清零。`no_progress` 达到 3 时，生成报告（outcome 为 None），状态设为 `incomplete`，返回 `finished`；否则返回 `("continue", facts_message(row))`。
  - `finish(service, db, row, outcome)`：调用 `evidence.report(service, db, row, narrative=候选 summary, outcome=outcome)`，设置 `row.outcome = outcome`、`row.state = "completed"`，写一条 `task_finished` 事件，内容为 `{"state", "outcome", "verdict": "not_comparable"}`。
  - 预算耗尽（worker 的 `finally` 里 `budget_stopped` 为真时）：`reviews` 中有记录就是 `completed_with_issues`；当前存在待复核候选但还没有复核结果，就是 `review_unavailable`，并写入 `reason: "budget_exhausted"`；两种情况都不满足时维持现有行为（outcome 为 None）。
- worker 的主循环：`bridge.run(config, prompt, callback)` 返回后，如果 `row.state == "running"`，就调用 `after_turn`。返回 `continue` 时，设置 `config["session_file"] = row.session_file`，把 `config["limits"]` 更新为当前的 `service.remaining(row)`，用新的 prompt 再次调用 `bridge.run`。返回 `finished` 或任务状态不再是 running 时，退出循环。复核阶段计费的 `account`：把消息交给 worker 现有的 `callback`，但要跳过对 `ready`/`result` 的会话记录（新增参数 `role="review"`，在 callback 中判断）。
- 恢复：worker 启动时，如果 `candidate.review_status == "pending"`，就先执行 `after_turn`，不先启动主会话。

- [ ] **Step 1: 写失败测试**（`test_driver.py`，主会话和复核会话都用脚本化的 Bridge）
  - `test_pass_gives_verified`：主会话提交候选，复核给出 pass，结果为 `outcome == "verified"`、`state == "completed"`，`report["outcome"] == "verified"`，并且 `report["verdict"] == "not_comparable"`。
  - `test_issues_then_repair_then_verified`：第一次复核给出 issues，续写消息中包含 finding 的原文，`repair_round == 1`；Pi 在 r1 重新登记并提交，第二次复核给出 pass，结果为 `verified`。
  - `test_rounds_exhausted_gives_completed_with_issues`：`max_repair_rounds=1`，两次复核都是 issues，结果为 `completed_with_issues`。
  - `test_zero_repair_rounds_finishes_on_first_issues`。
  - `test_review_failure_retries_once_then_unavailable`：复核会话两次都没有调用 `submit_review`，结果为 `review_unavailable`，`attempts == 2`。
  - `test_no_progress_three_turns_incomplete`：主会话每轮什么也不做，第 4 次结束时 `state == "incomplete"`、`outcome is None`，共产生 3 条续写消息。
  - `test_facts_message_has_no_advice`：续写消息中不包含 `"you should"`、`"next step"`、`"建议"`。
  - `test_budget_exhausted_during_review_gives_review_unavailable`：存在待复核候选、没有复核结果时预算耗尽，结果为 `review_unavailable`，`reason == "budget_exhausted"`。
  - `test_resume_with_pending_review_reruns_same_snapshot`：复核尚未完成时 worker 退出，恢复后针对同一个 `snapshot_sha256` 复核，`repair_round` 不变，也只有一条 `candidate_frozen` 事件。
- [ ] **Step 2: 运行测试，确认失败**
- [ ] **Step 3: 实现，并修改旧测试中的过时断言**
- [ ] **Step 4: 运行 `tests/unit/autonomous`、`tests/integration/autonomous/test_api.py`，以及两个不变量测试，确认全部通过**
- [ ] **Step 5: 提交** `feat(autonomous): drive turns through review, repair rounds and outcomes`

### Task 6: 安全终止与旧任务

**Files:**
- Modify: `packages/orchestration/p2r_orchestration/autonomous/worker.py`（异常处理）
- Modify: `packages/orchestration/p2r_orchestration/autonomous/control.py`（恢复和控制入口）
- Test: `tests/unit/autonomous/test_driver.py`、`tests/unit/autonomous/test_service.py`

**Interfaces:**
- Produces:
  - `SAFETY_CODES = {"isolation_changed", "network_isolation_failed", "host_addresses_changed"}`，定义在 `driver.py`。这些错误码来自 `p2r_sandbox/autonomous/execution.py:192/209/263/301` 的 `ExecutionError`。
  - `block_for_safety(service, db, row, error)`：调用 `service.request_cancel(row, error)`，设置 `row.state = "stopping"`、`row.outcome = "blocked_by_safety"`，写一条 `safety_blocked` 事件。提交事务后，调用者负责 `bridge.abort()` 和 `service.finish_stop(task_id)`。最终状态是 `stopped`，并保留 `outcome`。
  - 恢复接口遇到以下两种情况返回 409：`harness_json is None` 时返回 `LEGACY_TASK`，消息为"该任务创建于新版 harness 之前，请新建任务"；`outcome` 不为空时返回 `TASK_FINISHED`。
- 拦截的位置有两处：`dispatch` 抛出的工具异常，以及 worker 在 probe/prepare 阶段抛出的异常。只要 `code in SAFETY_CODES`，就走 `block_for_safety`，不再像现在这样设为 `paused`。

- [ ] **Step 1: 写失败测试**
  - `test_isolation_failure_blocks_for_safety`：Target 的 `start` 抛出 `code="network_isolation_failed"` 的异常，结果为 `outcome == "blocked_by_safety"`、`state == "stopped"`，并产生一条 `safety_blocked` 事件。
  - `test_prepare_isolation_failure_blocks_for_safety`：同样的异常改由 `prepare` 抛出，结果相同。
  - `test_blocked_task_cannot_resume`：返回 409 `TASK_FINISHED`。
  - `test_legacy_task_cannot_resume`：`harness_json=None` 的 paused 任务调用 resume，返回 409 `LEGACY_TASK`，`state` 仍为 `paused`。
  - `test_legacy_task_still_exports`：`export_zip` 能正常返回 zip。
  - `test_secret_in_artifact_does_not_terminate`：仍然只是本次调用报错，`outcome is None`。
- [ ] **Step 2: 运行测试，确认失败**
- [ ] **Step 3: 实现**
- [ ] **Step 4: 运行全部 `tests/unit` 和 `tests/integration/autonomous/test_api.py`，确认通过**
- [ ] **Step 5: 提交** `feat(autonomous): terminate on isolation failure and refuse legacy resume`

### Task 7: 同步设计文档与架构文档

**Files:**
- Modify: `docs/superpowers/specs/2026-11-05-autonomous-only-research-stages-design.md`
  - 5.5：把"在 `submit_candidate` 调用内同步完成"改为"在提交候选的那一轮结束后由程序执行，findings 通过续写消息送达"。原因是 harness 单次工具调用的超时上限为 300 秒（`apps/harness/bridge.mjs:305`）。
  - 5.2：把"新增 `repair_round` 与快照哈希列"改为"`outcome` 列 + `harness_json`"。
- Modify: `docs/architecture.md`：在自主模式一节补充这条链路：候选 → 复核 → 结局，以及新增的模块 `candidate.py`、`review.py`、`driver.py`。

- [ ] **Step 1: 修改文档**
- [ ] **Step 2: 提交** `docs: align design and architecture with post-turn review`

---

## 本计划之后

另写三份计划：
- 前端：只保留自主模式；新建任务流程；工作台展示自报进度、轮次、findings 和结局。
- 删除传统模式。
- Windows 移植。

每份计划在前一份合并之后再写，以便使用最新的接口。
