# Paper2Run Phase 0/1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first runnable Paper2Run backend loop: a FastAPI control plane with durable domain records, a local immutable ArtifactStore, a restricted persistent Python kernel, checkpoint/recovery metadata, and auditable SSE events.

**Architecture:** Keep the API process as a control plane only. Domain/application services validate requests, enforce session permissions and budgets, append Action records, and delegate code execution to a kernel supervisor through a narrow typed port. PostgreSQL is the production persistence target; SQLite is supported for deterministic local tests. Large outputs are written to an immutable local ArtifactStore and only metadata is persisted.

**Tech Stack:** Python 3.11/3.12, FastAPI, Pydantic 2, SQLAlchemy 2, Alembic, pytest, SQLite/PostgreSQL, standard-library subprocess/multiprocessing only for the first kernel implementation.

---

## Scope and file map

Create these focused modules:

- `pyproject.toml`: backend dependencies, test/lint entry points.
- `services/api/p2r_api/main.py`: FastAPI app assembly and routes.
- `services/api/p2r_api/settings.py`: environment-backed safe defaults.
- `services/api/p2r_api/dependencies.py`: request id, database session, actor context.
- `services/api/p2r_api/routes/sessions.py`: session, cell, action, checkpoint and SSE endpoints.
- `packages/contracts/p2r_contracts/common.py`: IDs, enums, envelope/error models.
- `packages/contracts/p2r_contracts/sessions.py`: request/response DTOs.
- `packages/persistence/p2r_persistence/db.py`: engine/session factory and UoW helpers.
- `packages/persistence/p2r_persistence/models.py`: SQLAlchemy tables and constraints.
- `packages/persistence/p2r_persistence/repositories.py`: narrow persistence operations.
- `packages/persistence/p2r_persistence/migrations/`: Alembic environment and initial migration.
- `packages/artifacts/p2r_artifacts/store.py`: immutable local content-addressed storage.
- `services/kernel/p2r_kernel/protocol.py`: typed supervisor request/result protocol.
- `services/kernel/p2r_kernel/supervisor.py`: lifecycle and timeout wrapper.
- `services/kernel/p2r_kernel/worker.py`: restricted child-process cell executor.
- `packages/orchestration/p2r_orchestration/service.py`: application service and state transitions.
- `tests/unit/phase01/`: domain, artifact, kernel and security tests.
- `tests/integration/phase01/`: API, persistence and recovery tests.

Do not implement ExperimentSpec/Revision, Runner, Evaluator, model gateway, remote GPU, or frontend in this loop.

### Task 1: Bootstrap and importable package skeleton

**Files:** Create `pyproject.toml`, package `__init__.py` files, and the module paths listed above.

- [ ] Write a failing smoke test that imports `p2r_api.main:create_app` and checks the health route contract.
- [ ] Run `pytest tests/integration/phase01/test_bootstrap.py -q`; expect import failure before implementation.
- [ ] Add minimal package metadata and dependencies: `fastapi`, `uvicorn`, `pydantic>=2`, `pydantic-settings`, `sqlalchemy>=2`, `alembic`, `aiosqlite`, `httpx`, `pytest`, `pytest-asyncio`.
- [ ] Implement `create_app()` with `GET /healthz` returning `{status: "ok", service: "p2r-api"}` and an `X-Request-ID` response header.
- [ ] Run the smoke test and `python -m compileall services packages`; expect PASS and no syntax errors.

### Task 2: Contracts and state-machine invariants

**Files:** `packages/contracts/p2r_contracts/common.py`, `sessions.py`, `tests/unit/phase01/test_contracts.py`.

- [ ] Add tests for valid session/cell requests, maximum source size, forbidden capability names, and legal/illegal state transitions.
- [ ] Define string enums for `SessionStatus`, `ActionStatus`, `CellStatus`, `TrustClass`, `PermissionDecision`, and `RecoveryStatus`.
- [ ] Define DTOs: `SessionCreate`, `SessionRead`, `CellCreate`, `CellRead`, `ActionRead`, `CheckpointCreate`, `CheckpointRead`, `EventRead`, `Ok[T]`, and `ApiError` with `extra="forbid"` on untrusted request models.
- [ ] Implement canonical JSON using sorted keys and compact separators; expose `sha256_canonical(value)` and verify the hash in tests.
- [ ] Run `pytest tests/unit/phase01/test_contracts.py -q`.

### Task 3: Persistence schema and migration

**Files:** `packages/persistence/p2r_persistence/db.py`, `models.py`, `repositories.py`, Alembic files, `tests/integration/phase01/test_persistence.py`.

- [ ] Test creation of one session, ordered actions, cell execution, environment, artifact metadata, checkpoint manifest, and events; test unique `(session_id, sequence_no)` and idempotency conflicts.
- [ ] Create tables `sessions`, `actions`, `cell_executions`, `artifacts`, `environments`, `checkpoints`, `events`, and `idempotency_keys` with UUID/string IDs, UTC timestamps, JSON payloads, SHA-256 columns, and foreign keys.
- [ ] Make actions/events append-only at the repository boundary: expose insert/list methods, no update/delete methods for historical records.
- [ ] Implement SQLite test setup and PostgreSQL-compatible SQLAlchemy types without database-specific business logic.
- [ ] Add an Alembic base and initial migration that creates the same schema.
- [ ] Run `pytest tests/integration/phase01/test_persistence.py -q` and `alembic upgrade head` against a temporary SQLite database.

### Task 4: Immutable ArtifactStore and environment capture

**Files:** `packages/artifacts/p2r_artifacts/store.py`, `packages/orchestration/p2r_orchestration/environment.py`, tests.

- [ ] Test that storing identical bytes returns the same SHA-256 address, replacing an existing address with different bytes raises an integrity error, and path traversal is impossible.
- [ ] Implement `LocalArtifactStore.put_bytes`, `open_bytes`, and `exists` under a configured root using hash-based filenames and atomic create/rename.
- [ ] Persist only artifact metadata in SQLAlchemy, including media type, byte size, sha256, producer action, trust class, and provenance JSON.
- [ ] Capture a redacted environment summary containing runtime, Python version, platform, package lock hash if supplied, and hardware summary; never include secrets or full environment variables.
- [ ] Run the focused artifact/environment tests.

### Task 5: Restricted Python Kernel Supervisor

**Files:** `services/kernel/p2r_kernel/protocol.py`, `worker.py`, `supervisor.py`, tests.

- [ ] Test structured result capture, stdout/stderr capture, timeout termination, source-size rejection, forbidden `subprocess`/socket/import behavior, and namespace hash changes.
- [ ] Define `ExecuteCellRequest` and `ExecuteCellResult` as typed models; results contain status, stdout, stderr, JSON-safe result summary, namespace hash, and error code.
- [ ] Implement a child process executor with a fresh restricted builtins/import allowlist, no `subprocess`, no socket, no filesystem writes outside the supplied workspace, bounded stdout/stderr, and wall-time timeout. The supervisor must terminate the child on timeout and report `CELL_TIMEOUT`.
- [ ] Keep this restriction explicit: it is a first local defense, not a claim of production-grade hostile-code sandboxing; production deployment must add OS/container isolation before untrusted code is enabled.
- [ ] Run kernel security tests and `python -m compileall services/kernel`.

### Task 6: Session application service and API loop

**Files:** `packages/orchestration/p2r_orchestration/service.py`, `services/api/p2r_api/routes/sessions.py`, dependencies, tests.

- [ ] Test create/list/get session, submit cell with idempotency replay/conflict, viewer/read-only rejection, ordered action ledger, artifact registration, and checkpoint creation.
- [ ] Implement service methods: `create_session`, `list_sessions`, `submit_cell`, `list_actions`, `create_checkpoint`, `list_checkpoints`, and `get_events`.
- [ ] For each cell: validate session state/permission/budget; reserve an action sequence; insert Action and CellExecution; execute through supervisor; store stdout/stderr/result artifacts; insert Environment; finalize status and event in one short transaction where possible.
- [ ] Implement endpoints under `/api/v1/projects/{project_id}/sessions` and `/api/v1/sessions/{session_id}` from the approved design, plus `GET /events` as `text/event-stream` with a bounded `after` cursor.
- [ ] Return the standard `Ok[T]` envelope and stable error codes; all writes require `Idempotency-Key`.
- [ ] Run API integration tests using `httpx.AsyncClient` and SQLite.

### Task 7: Checkpoint and recovery classification

**Files:** `packages/orchestration/p2r_orchestration/recovery.py`, routes/service updates, tests.

- [ ] Test complete recovery when manifest, artifacts, environment and action cursor validate; partial recovery when ledger/artifacts exist but kernel must be rebuilt; failed recovery on hash mismatch or missing required artifact.
- [ ] Store a checkpoint manifest artifact containing action cursor, namespace snapshot hash, input artifact IDs, environment ID, replay plan, and manifest hash.
- [ ] Implement deterministic `classify_recovery()` returning only `recovered`, `partial`, or `failed` with explicit missing/invalid reasons.
- [ ] Implement `POST /api/v1/sessions/{session_id}/restore` as metadata validation plus supervisor restart; never silently claim that an unverified namespace was recovered.
- [ ] Run recovery tests and verify the API exposes the classification and reasons.

### Task 8: Self-check gate and handoff

**Files:** `Makefile` or documented commands, `docs/verification/phase01.md`, final tests.

- [ ] Run `pytest -q`, `python -m compileall services packages`, `git diff --check`, and an OpenAPI generation smoke check.
- [ ] Run a deterministic end-to-end test: create session → submit allowed cell → inspect action/cell/artifacts/environment → create checkpoint → classify recovery → subscribe events.
- [ ] Run negative tests for forbidden capability, oversized source, viewer action, idempotency body conflict, timeout, path traversal, and fake metric/`matched` fields.
- [ ] Record exactly what is proven and explicitly mark PostgreSQL, hostile-code isolation, real training, remote GPU, and Evaluator as out of scope for this loop.
- [ ] Commit only the implementation and plan files after all checks pass; do not include the pre-existing `.superpowers/` directory.

## Self-review checklist

- [ ] Every Phase 0/1 requirement has a task and test evidence.
- [ ] No task introduces a Runner, Evaluator, model call, remote GPU, or frontend.
- [ ] API paths remain under `/api/v1`; internal kernel is not exposed to the browser.
- [ ] `run.status`/`verdict` are not conflated; this loop does not create a verdict.
- [ ] Outputs are treated as untrusted and only metadata/hashes enter trusted records.
- [ ] Recovery distinguishes `recovered`, `partial`, and `failed`.
- [ ] No secret is logged or persisted.
