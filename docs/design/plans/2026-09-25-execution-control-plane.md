# Execution Control Plane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the controlled Execution lifecycle with fenced heartbeat, finish, recovery, cancellation, HTTP query/control APIs, and regression coverage without enabling real shell/model/GPU execution.

**Architecture:** Keep `ExecutionRecord` as the public execution state and reuse the existing `Job` lease/fencing token as the authority. HTTP runner callbacks authenticate by runner identity plus execution fencing token; every mutation is conditional and returns conflict on stale ownership. Recovery marks expired executions as recovered and requeues the matching job only when the job still carries the same fence. Run cancellation becomes cooperative: queued jobs are cancelled before execution, while running executions are marked cancellation-requested through the parent Run and the runner must finish as cancelled.

**Tech Stack:** FastAPI, SQLAlchemy, SQLite test fixtures, pytest, existing `JobRepository` fencing primitives.

---

### Task 1: Execution repository regression contract

**Files:**
- Modify: `packages/orchestration/p2r_orchestration/execution_repository.py`
- Test: `tests/unit/phase02/test_execution_repository.py`

- [x] **Step 1: Write failing tests** for start/heartbeat/finish stale-fence rejection, expired recovery requeue, and recovery idempotence.
- [x] **Step 2: Run the focused tests and verify they fail for the missing or incorrect behavior.**
- [x] **Step 3: Implement the smallest repository changes: normalize terminal status handling, make recovery conditional on current running state, and preserve transactional rollback when job fencing rejects requeue/failure.**
- [x] **Step 4: Run the focused tests and existing job fencing tests.**

### Task 2: Runner control HTTP API

**Files:**
- Modify: `services/api/p2r_api/routes/runs.py`
- Modify: `services/api/p2r_api/main.py` only if router registration is required
- Test: `tests/integration/phase02/test_executions_http.py`

- [x] **Step 1: Write failing tests** for execution listing whitelist/cross-project isolation, heartbeat success and stale-fence conflict, finish success/failure and stale-fence conflict.
- [x] **Step 2: Run the focused HTTP tests and verify they fail because the control routes do not exist.**
- [x] **Step 3: Implement request models and routes under `/api/v1/executions/{execution_id}/heartbeat` and `/api/v1/executions/{execution_id}/finish`; resolve the execution, enforce project membership through its Run, require runner identity and non-secret fence, mutate only through `ExecutionRepository`, and return a stable envelope.
- [x] **Step 4: Run focused HTTP tests plus attempts and existing identity isolation tests.**

### Task 3: Controlled runner heartbeat and cancellation handoff

**Files:**
- Modify: `services/runner/p2r_runner_service.py`
- Modify: `packages/orchestration/p2r_orchestration/run_worker.py`
- Test: `tests/unit/phase02/test_controlled_runner.py`
- Test: `tests/integration/phase02/test_run_worker.py`

- [x] **Step 1: Write failing tests** proving the runner sends a heartbeat hook before finish and converts a cancellation request into a cancelled execution/job result.
- [x] **Step 2: Run the focused tests and verify they fail.**
- [x] **Step 3: Add an optional heartbeat callback and cancellation probe to `ControlledRunner`; keep callbacks bounded and do not expose provider secrets or execute arbitrary commands.**
- [x] **Step 4: Run runner and worker tests and confirm stale heartbeat aborts before business completion.**

### Task 4: Cooperative cancellation and terminal job protection

**Files:**
- Modify: `services/api/p2r_api/routes/runs.py`
- Modify: `packages/orchestration/p2r_orchestration/run_worker.py`
- Test: `tests/integration/phase02/test_run_worker.py`
- Test: `tests/integration/phase02/test_executions_http.py`

- [x] **Step 1: Write failing tests** for cancelling a queued run, cancelling a running run, idempotent cancellation, and stale completion after cancellation.
- [x] **Step 2: Run the tests and verify the expected failures.**
- [x] **Step 3: Implement conditional cancellation: cancel queued jobs immediately; for running executions set `cancel_requested` without allowing the request to overwrite completed/failed/cancelled state; ensure worker completion observes the request and records cancelled.**
- [x] **Step 4: Run the full phase02 integration suite.**

### Task 5: Full verification and residual-risk record

**Files:**
- Modify: `docs/design/plans/2026-09-25-execution-control-plane.md` only for checkboxes/results

- [x] **Step 1: Run `.venv/bin/python -m pytest -q`.**
- [x] **Step 2: Run `.venv/bin/python -m compileall -q packages services tests`.**
- [x] **Step 3: Run `git diff --check`.**
- [x] **Step 4: Confirm no commit, push, dependency installation, network access, or external deployment occurred; record PostgreSQL/real runner/GPU as external prerequisites if still unavailable.**

**Verification result (2026-09-25):** 249 tests passed with one pre-existing Starlette/httpx deprecation warning; compileall and `git diff --check` passed. The implementation remains limited to SQLite/controlled fixture coverage. PostgreSQL lock semantics, multi-worker concurrency, real Runner/process cancellation, provider/GPU execution, and external deployment remain prerequisites for a production-level acceptance run.**
