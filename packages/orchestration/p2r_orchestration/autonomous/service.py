"""Durable autonomous control plane; model decisions live in the Pi SDK."""
from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time
from uuid import uuid4

from sqlalchemy import select

from p2r_contracts.autonomous import resolve_project_config
from p2r_evaluation.autonomous import freeze_protocol
from p2r_evaluation.reference_verification import validate_reference_values
from p2r_orchestration.research_capabilities import CapabilityRegistry
from p2r_persistence.autonomous import AutonomousConfigRecord as Config, AutonomousTaskRecord as Task, AutonomousEventRecord as Event
from p2r_persistence.models import ExtractionRecord, PaperRecord, PaperVersionRecord, PaperTextRecord, utcnow
from .bridge import HarnessBridge
from .control import ControlMixin
from .security import ProfileStore, digest, fail


class AutonomousService(ControlMixin):
    def __init__(self, session_factory, root=None, *, artifact_store=None, target_factory=None,
                 bridge_factory=None, registry=None, environment_grants=()):
        self.sessions = session_factory
        self.root = Path(root or os.getenv("P2R_AUTONOMOUS_DIR") or
                         Path(os.getenv("P2R_DEMO_DIR", ".p2r-demo")) / "autonomous").resolve()
        self.lock = threading.RLock()
        self.profiles = ProfileStore(environment_grants=environment_grants)
        self.registry = registry or CapabilityRegistry(imported_root=self.root / "skills")
        self.artifact_store = artifact_store
        self.target_factory = target_factory
        self.bridge_factory = bridge_factory or HarnessBridge
        self.workers = {}
        self.closing = False
        self._lease = None

    def task_root(self, task_id):
        # IDs originate in the DB, never interpret a caller's path.
        if len(task_id) != 36 or any(c not in "0123456789abcdef-" for c in task_id):
            fail("RESOURCE_NOT_FOUND", "task not found", 404)
        return self.root / "tasks" / task_id

    def row(self, db, task_id):
        row = db.get(Task, task_id)
        if row is None:
            fail("RESOURCE_NOT_FOUND", "task not found", 404)
        return row

    def public(self, row):
        snapshot = dict(row.config_snapshot)
        # SDK profile endpoint is public metadata; reference values contain no credentials.
        return self.profiles.clean({"id": row.id, "project_id": row.project_id,
            "state": row.state, "objective": row.objective, "config_snapshot": snapshot,
            "usage": row.usage_json, "created_at": row.created_at.isoformat(),
            "updated_at": row.updated_at.isoformat(), "report": row.report_json,
            "error": row.error_json, "pending_decision": row.pending_decision_json}, bounded=False)

    def event(self, db, row, kind, payload):
        row.event_sequence += 1
        row.updated_at = utcnow()
        db.add(Event(task_id=row.id, sequence=row.event_sequence, type=kind,
                     payload=self.profiles.clean(payload)))

    def latest_config(self, db, project_id):
        return db.scalar(select(Config).where(Config.project_id == project_id).order_by(Config.version.desc()))

    def put_config(self, db, project_id, config, if_match):
        previous = self.latest_config(db, project_id)
        if previous and str(if_match or "").strip('"') != str(previous.version):
            fail("PRECONDITION_FAILED", "If-Match must match the current config version", 412)
        if config.get("project", {}).get("id") != project_id:
            fail("VALIDATION_ERROR", "configuration project ID must match the route")
        # Experiment mounts are outside the control plane; config cannot select arbitrary host directories.
        project_root = self.root.parent / "autonomous-workspaces" / digest(project_id)[:24]
        try:
            resolved = resolve_project_config(config, project_root)
        except (ValueError, TypeError):
            fail("VALIDATION_ERROR", "invalid project configuration or workspace")
        workspace = Path(resolved["project"]["workspace"])
        if workspace == project_root or self.root.is_relative_to(workspace) or workspace.is_relative_to(self.root):
            fail("VALIDATION_ERROR", "workspace must be a child of the project workspace root and outside control state")
        record = Config(project_id=project_id, version=(previous.version + 1 if previous else 1), config_json=resolved)
        db.add(record)
        db.flush()
        return {"version": record.version, "config": record.config_json}

    def paper_context(self, db, project_id, version_id):
        if version_id:
            version = db.get(PaperVersionRecord, version_id)
            paper = db.get(PaperRecord, version.paper_id) if version else None
            if not paper or paper.project_id != project_id:
                fail("RESOURCE_NOT_FOUND", "paper version not found", 404)
        else:
            paper = db.scalar(select(PaperRecord).where(PaperRecord.project_id == project_id).order_by(PaperRecord.created_at.desc()))
            version = db.scalar(select(PaperVersionRecord).where(PaperVersionRecord.paper_id == paper.id).order_by(PaperVersionRecord.version.desc())) if paper else None
        if not paper:
            return {"paper_available": False, "limitations": ["No project paper has been uploaded."]}
        sha = version.sha256 if version else paper.sha256
        context = {"paper_available": True, "paper_id": paper.id, "paper_version_id": version.id if version else None,
                   "source_sha256": sha, "title": paper.title, "limitations": []}
        if version and version.source_kind == "text" and self.artifact_store:
            try:
                import hashlib
                raw = self.artifact_store.open_bytes(version.sha256)
                if len(raw) != version.bytes or hashlib.sha256(raw).hexdigest() != version.sha256:
                    raise ValueError("paper source integrity mismatch")
                decoded = raw.decode("utf-8")
                context.update({"text_sha256": version.sha256, "text": decoded[:120000],
                                "text_truncated": len(decoded) > 120000, "source_bytes": len(raw),
                                "source_characters": len(decoded), "text_limit_characters": 120000})
            except (OSError, ValueError):
                context["limitations"].append("Text paper source could not be verified.")
        text = db.scalar(select(PaperTextRecord).where(PaperTextRecord.paper_id == paper.id,
            PaperTextRecord.source_sha256 == sha, PaperTextRecord.status == "ready").order_by(PaperTextRecord.created_at.desc()))
        if text and text.text_sha256 and self.artifact_store:
            try:
                raw = self.artifact_store.open_bytes(text.text_sha256)
                decoded = raw.decode("utf-8")
                context.update({"text_sha256": text.text_sha256, "text": decoded[:120000],
                                "text_truncated": len(decoded) > 120000, "source_bytes": len(raw),
                                "source_characters": len(decoded), "text_limit_characters": 120000})
            except (OSError, ValueError):
                context["limitations"].append("Parsed paper text could not be verified.")
        extraction = db.scalar(select(ExtractionRecord).where(ExtractionRecord.paper_id == paper.id,
            ExtractionRecord.status == "ready").order_by(ExtractionRecord.created_at.desc()))
        if extraction:
            payload = extraction.payload_json
            refs = payload.get("source_refs") or []
            if refs and all(ref.get("pdf_sha256") == sha for ref in refs if isinstance(ref, dict)):
                context["extraction"] = payload
        if not context.get("text") and not context.get("extraction"):
            context["limitations"].append("No verified parsed or extracted paper content is available; no fixture substituted.")
        return self.profiles.clean(context, bounded=False)

    def create_task(self, db, project_id, actor, payload):
        if payload.get("authorization", {}).get("confirmed") is not True:
            fail("AUTHORIZATION_REQUIRED", "explicit experiment authorization is required")
        config = self.latest_config(db, project_id)
        if not config:
            fail("CONFIG_REQUIRED", "configure the project before creating a task", 409)
        snapshot = json.loads(json.dumps(config.config_json))
        snapshot["config_version"] = config.version
        snapshot["model_profile"] = self.profiles.snapshot(actor, snapshot["model"]["provider_profile"], snapshot["model"].get("credential_ref"))
        if snapshot.get("specialists"):
            snapshot["specialist_profiles"] = {
                item["id"]: self.profiles.snapshot(actor, item["model"]["provider_profile"], item["model"].get("credential_ref"))
                for item in snapshot["specialists"]
            }
        skills = self.registry.snapshot(snapshot["harness"]["skills"])
        protocol = freeze_protocol(payload["protocol"]) if payload.get("protocol") else None
        reference_values = payload.get("reference_values")
        reference_scope = payload.get("reference_scope")
        references_confirmed = payload.get("reference_sources_confirmed") is True
        if reference_values is not None:
            if protocol is None or not reference_scope or reference_scope != reference_scope.strip() or not references_confirmed:
                fail("VALIDATION_ERROR", "reference values require a frozen protocol, scope and explicit source confirmation")
            try:
                reference_values = validate_reference_values(reference_values)
            except (ValueError, TypeError):
                fail("VALIDATION_ERROR", "invalid reference values")
        elif reference_scope is not None or references_confirmed:
            fail("VALIDATION_ERROR", "reference values are required with reference metadata")
        row = Task(project_id=project_id, actor_id=actor, state="queued", objective=payload["objective"],
            config_snapshot=snapshot, context_json=self.paper_context(db, project_id, payload.get("paper_version_id")),
            reference_values_json=reference_values, reference_scope=reference_scope,
            reference_sources_confirmed=references_confirmed,
            skills_json=skills, protocol_json=protocol, usage_json={"calls": 0, "input_tokens": 0, "output_tokens": 0,
            "total_tokens": 0, "wall_time_seconds": 0}, jobs_json={}, artifacts_json=[], event_sequence=0)
        db.add(row)
        db.flush()
        if row.context_json.get("paper_available") and not row.context_json.get("text"):
            row.state = "paused"
            row.error_json = {"code": "paper_content_required", "message": "Verified paper text is unavailable; finish parsing before resume."}
        self.event(db, row, "task_created", {"authorization_confirmed": True, "config_version": config.version,
                                            "protocol_sha256": protocol["sha256"] if protocol else None})
        return row

    def resolve_models(self, row):
        """Resolve only frozen identities; secrets live solely in bridge input memory."""
        snapshot = row.config_snapshot
        main = self.profiles.resolve(row.actor_id, snapshot["model"]["provider_profile"], snapshot["model_profile"])
        specialists = []
        for item in snapshot.get("specialists", []):
            frozen = snapshot.get("specialist_profiles", {}).get(item["id"])
            if not frozen:
                fail("MODEL_SNAPSHOT_REQUIRED", "specialist frozen profile is missing", 409)
            specialists.append({"id": item["id"], "description": item["description"], "task_types": item["task_types"],
                "model": self.profiles.resolve(row.actor_id, item["model"]["provider_profile"], frozen)})
        return main, specialists

    def events(self, db, task_id, after=0):
        self.row(db, task_id)
        rows = db.scalars(select(Event).where(Event.task_id == task_id, Event.sequence > after)
                          .order_by(Event.sequence).limit(200)).all()
        return {"events": [{"sequence": r.sequence, "type": r.type, "payload": r.payload,
                "created_at": r.created_at.isoformat()} for r in rows],
                "next_cursor": rows[-1].sequence if rows else after}

    def settle_interrupted_usage(self, db, row):
        from datetime import datetime
        usage = dict(row.usage_json)
        if "active_since" not in usage and "pending_token_reservation" not in usage:
            return
        active_since = usage.pop("active_since", None)
        if active_since:
            elapsed = max(0, (utcnow() - datetime.fromisoformat(active_since)).total_seconds())
            usage["wall_time_seconds"] = usage.get("wall_time_seconds", 0) + elapsed
        reservation = usage.pop("pending_token_reservation", 0)
        if reservation:
            usage["total_tokens"] = usage.get("total_tokens", 0) + reservation
            usage["unverified_token_reservation"] = usage.get("unverified_token_reservation", 0) + reservation
        row.usage_json = usage
        self.event(db, row, "interrupted_usage_settled", {"reserved_tokens": reservation})

    def recover(self):
        # A local runtime owns this control directory. Fail closed for competing
        # API processes instead of resetting another process's active tasks.
        with self.lock:
            if self._lease is None:
                self.root.mkdir(parents=True, exist_ok=True)
                lease = (self.root / "runtime.lock").open("a+b")
                try:
                    if os.name == "nt":
                        import msvcrt
                        lease.seek(0); lease.write(b"0"); lease.flush(); lease.seek(0)
                        msvcrt.locking(lease.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    lease.close()
                    fail("RUNTIME_ALREADY_ACTIVE", "another autonomous runtime owns this control directory", 409)
                self._lease = lease
        with self.lock, self.sessions() as db:
            rows = db.scalars(select(Task)).all()
            for row in rows:
                # A pause/exit state can commit before the worker's finally runs.
                # Settle markers in every state atomically and exactly once.
                self.settle_interrupted_usage(db, row)
                if row.state in ("queued", "running", "stopping"):
                    if row.state == "stopping":
                        self.request_cancel(row)
                    row.state = "paused"
                    row.error_json = (self.cancellation_error(row, {"code": "stop_unconfirmed", "message": "Restart interrupted cancellation; replay stop to reconcile."})
                                      if self.cancel_requested(row) else
                                      {"code": "recovery_required", "message": "Runtime restarted; review jobs and explicitly resume."})
                    self.event(db, row, "recovery_required", {"jobs": row.jobs_json})
            db.commit()
        self.closing = False

    def launch(self, task_id, message=None):
        from .worker import run_worker
        with self.lock:
            if self.closing:
                fail("RUNTIME_CLOSING", "runtime is shutting down", 409)
            previous = self.workers.get(task_id)
            if previous and previous["thread"].is_alive():
                fail("TASK_BUSY", "previous worker has not finished pausing", 409)
            bridge = self.bridge_factory()
            thread = threading.Thread(target=run_worker, args=(self, task_id, bridge, message),
                                      daemon=True, name=f"autonomous-{task_id[:8]}")
            self.workers[task_id] = {"thread": thread, "bridge": bridge, "target": None}
            thread.start()

    def make_target(self, row):
        if self.target_factory is None:
            from p2r_sandbox.autonomous import ExecutionTarget
            factory = ExecutionTarget
        else:
            factory = self.target_factory
        c = row.config_snapshot
        target = c["target"]
        # Every task gets its own mount and idempotent execution state directory.
        workspace = ((target["remote_workspace"].rstrip("/") + "/" + row.id) if target["kind"] == "ssh"
                     else str(Path(c["project"]["workspace"]) / row.id))
        config = {"kind": target["kind"], "workspace": workspace, "image": target["image"],
                  "gpu_devices": c["limits"]["gpu_devices"], "storage_bytes": c["limits"]["storage_bytes"],
                  "network": c["network"]}
        if target["kind"] == "ssh":
            config["ssh_alias"] = target["ssh_config_alias"]
        return factory(config, self.task_root(row.id) / "execution")

    def remaining(self, row):
        limits, usage = row.config_snapshot["limits"], row.usage_json
        remaining = {"max_model_calls": limits["max_model_calls"] - usage.get("calls", 0),
                     "max_model_tokens": limits["max_model_tokens"] - usage.get("total_tokens", 0),
                     "wall_time_seconds": limits["wall_time_seconds"] - usage.get("wall_time_seconds", 0)}
        if any(value <= 0 for value in remaining.values()):
            fail("BUDGET_EXHAUSTED", "cumulative task budget exhausted", 409)
        remaining["wall_time_seconds"] = min(remaining["wall_time_seconds"], 2147483)
        return remaining

    def shutdown(self):
        with self.lock, self.sessions() as db:
            self.closing = True
            workers = list(self.workers.items())
            for task_id, worker in workers:
                if worker["thread"].is_alive():
                    row = self.row(db, task_id)
                    if row.state in ("running", "queued"):
                        row.state = "paused"
                        row.error_json = {"code": "recovery_required", "message": "Application exited; background jobs retained."}
                        self.event(db, row, "application_exit", {"background_jobs_retained": True})
                    worker["bridge"].abort()
            db.commit()
        deadline = time.monotonic() + 5
        for _, worker in workers:
            worker["thread"].join(timeout=max(0, deadline - time.monotonic()))
        with self.lock:
            if self._lease is not None and not any(w["thread"].is_alive() for _, w in workers):
                self._lease.close()
                self._lease = None
