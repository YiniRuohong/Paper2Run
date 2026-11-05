"""Explicit user controls, kept separate from SDK tools and protocol authority."""
from __future__ import annotations

import json
from uuid import uuid4

from .security import fail


class ControlMixin:
    @staticmethod
    def cancel_requested(row):
        return bool(row.context_json.get("cancel_intent", {}).get("requested"))

    @staticmethod
    def request_cancel(row, reason=None):
        # Trusted control metadata, not model-editable; independent of state so
        # an unknown stop result cannot erase cancellation or its budget cause.
        intent = {**row.context_json.get("cancel_intent", {}), "requested": True}
        if reason and not intent.get("reason"):
            intent["reason"] = reason
        row.context_json = {**row.context_json, "cancel_intent": intent}

    @staticmethod
    def cancellation_error(row, error=None):
        reason = row.context_json.get("cancel_intent", {}).get("reason")
        return {**error, "cause": reason} if error and reason else error or reason

    def control(self, db, task_id, action, message=None):
        row = self.row(db, task_id)
        worker = self.workers.get(task_id)
        active = bool(worker and worker["thread"].is_alive())
        if action == "message":
            if not message:
                fail("VALIDATION_ERROR", "message is required")
            self.event(db, row, "user_message", {"message": message})
            # Instructions survive a restart; do not mutate the initial paper context.
            context = dict(row.context_json)
            context["user_messages"] = [*context.get("user_messages", [])[-31:], message]
            row.context_json = context
            if active:
                worker["bridge"].steer(message)
            return row
        if action == "resume":
            if row.outcome is not None:
                fail("TASK_FINISHED", "task has a final outcome and cannot resume", 409)
            if row.state != "paused":
                fail("INVALID_STATE", "only paused tasks can be resumed", 409)
            if active:
                fail("TASK_BUSY", "wait for the previous worker to finish pausing", 409)
            if self.cancel_requested(row):
                fail("CANCELLATION_PENDING", "task cancellation is irrevocable; reconcile stop instead of resuming", 409)
            if row.harness_json is None:
                fail("LEGACY_TASK", "该任务创建于新版 harness 之前，请新建任务", 409)
            self.settle_interrupted_usage(db, row)
            if row.context_json.get("paper_available") and not row.context_json.get("text"):
                context = self.paper_context(db, row.project_id, row.context_json.get("paper_version_id"))
                if not context.get("text"):
                    fail("PAPER_CONTENT_REQUIRED", "finish verified paper parsing before resume", 409)
                row.context_json = {**row.context_json, **context}
            decision = row.pending_decision_json
            if decision:
                # The user must approve the precise frozen proposal, not merely resume.
                if message != f"approve:{decision['id']}":
                    fail("PROTOCOL_APPROVAL_REQUIRED", "resume message must be approve:<pending_decision.id>", 409)
                row.protocol_json = decision["proposed"]
                row.pending_decision_json = None
                row.reference_sources_confirmed = False
                self.event(db, row, "protocol_approved", {"decision_id": decision["id"], "protocol": row.protocol_json})
                row.report_json = None
            self.remaining(row)
            self.resolve_models(row)
            row.state, row.error_json = "queued", None
            self.event(db, row, "task_resumed", {"jobs_to_reconcile": row.jobs_json})
            return row
        if action == "pause":
            if row.state not in ("queued", "running", "paused"):
                fail("INVALID_STATE", "task cannot be paused", 409)
            row.state = "paused"
            self.event(db, row, "task_paused", {"background_jobs_retained": True})
            if active:
                worker["bridge"].abort()
            return row
        if action == "stop":
            self.request_cancel(row)
            if row.state == "stopped":
                return row
            row.state = "stopping"
            self.event(db, row, "stop_requested", {})
            if active:
                worker["bridge"].abort()
            # Persist control before potentially slow remote stop. Caller finalizes
            # stop after its transaction, so in-flight tool facts can finish safely.
            return row
        fail("VALIDATION_ERROR", "unsupported control action")

    def finish_stop(self, task_id):
        with self.lock, self.sessions() as db:
            row = self.row(db, task_id)
            if row.state == "stopped":
                return
            if row.state == "stopping":
                self.request_cancel(row)
            if not self.cancel_requested(row):
                return
            jobs = dict(row.jobs_json)
            if not jobs:
                row.state, row.error_json = "stopped", self.cancellation_error(row)
                self.event(db, row, "stop_observed", {"jobs": {}, "failures": []})
                db.commit()
                return
            db.commit()
            try:
                target = self.make_target(row)
            except (ImportError, OSError, ValueError) as exc:
                row.state = "paused"
                row.error_json = self.profiles.clean(self.cancellation_error(row, {"code": "stop_unconfirmed", "message": str(exc)}))
                self.event(db, row, "stop_observed", {"failures": [row.error_json]})
                db.commit()
                return
        errors, observations = [], {}
        try:
            for launch_id, info in jobs.items():
                job_id = info.get("job_id")
                if not job_id:
                    # Stable launch ID lets the target reconcile a lost launch response.
                    job_id = launch_id
                try:
                    observed = target.stop(job_id)
                    observations[launch_id] = observed
                    if observed.get("state") not in ("stopped", "cancelled", "canceled", "completed", "succeeded", "failed", "timed_out", "timeout", "exited"):
                        errors.append({"launch_id": launch_id, "error": "target did not confirm termination", "observation": observed})
                except Exception as exc:
                    errors.append({"launch_id": launch_id, "error": str(exc)})
        finally:
            target.close()
        with self.lock, self.sessions() as db:
            row = self.row(db, task_id)
            row.state = "paused" if errors else "stopped"
            row.error_json = self.profiles.clean(self.cancellation_error(row,
                {"code": "stop_unconfirmed", "failures": errors} if errors else None))
            self.event(db, row, "stop_observed", {"jobs": observations, "failures": errors})
            db.commit()

    def propose(self, db, row, proposed, reason):
        from p2r_evaluation.autonomous import freeze_protocol
        frozen = freeze_protocol(proposed)
        if row.protocol_json == frozen:
            return {"approved": True, "protocol_sha256": frozen["sha256"]}
        before = row.protocol_json["protocol"] if row.protocol_json else {}
        changed = sorted(k for k in frozen["protocol"] if frozen["protocol"][k] != before.get(k))
        # Only explicit top-level parameter field names are authorizable. Never let
        # an agent authorize its own evidence, budget, evaluator or policy changes.
        allowed = set(before.get("allowed_changes", [])) & {"parameters", "entrypoint"}
        if before and set(changed) <= allowed:
            row.protocol_json = frozen
            row.reference_sources_confirmed = False
            row.report_json = None
            self.event(db, row, "protocol_version", {"protocol": frozen, "reason": reason, "changed": changed})
            return {"approved": True, "protocol_sha256": frozen["sha256"]}
        decision = {"id": str(uuid4()), "kind": "protocol_approval", "proposed": frozen,
                    "changed": changed, "reason": reason}
        row.pending_decision_json, row.state = decision, "paused"
        self.event(db, row, "protocol_approval_required", decision)
        return {"approved": False, "paused": True, "pending_decision": decision}
