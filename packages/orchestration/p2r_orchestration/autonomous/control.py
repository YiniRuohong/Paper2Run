"""Explicit user controls, kept separate from SDK tools and protocol authority."""
from __future__ import annotations

import json
from uuid import uuid4

from .security import fail


def drop_auto_resume(row):
    """Invalidate a scheduled automatic resume (worker.schedule_auto_resume); the attempt count stays."""
    if row.harness_json and "auto_resume" in row.harness_json:
        row.harness_json = {k: v for k, v in row.harness_json.items() if k != "auto_resume"}


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

    def active_worker(self, task_id):
        worker = self.workers.get(task_id)
        return worker if worker and worker["thread"].is_alive() else None

    def deliver_message(self, db, row, message):
        """User steering: journaled, kept for the next prompt, and steered into a live turn."""
        self.event(db, row, "user_message", {"message": message})
        # Instructions survive a restart; do not mutate the initial paper context.
        context = dict(row.context_json)
        context["user_messages"] = [*context.get("user_messages", [])[-31:], message]
        row.context_json = context
        worker = self.active_worker(row.id)
        if worker:
            worker["bridge"].steer(message)

    def resume(self, db, task_id, settle=None):
        """The one resume path. `settle(row, pending_decision)` validates without mutating and
        returns a callable that applies the user's decision, or None when nothing is pending."""
        row = self.row(db, task_id)
        if row.outcome is not None:
            fail("TASK_FINISHED", "task has a final outcome and cannot resume", 409)
        if row.state != "paused":
            fail("INVALID_STATE", "only paused tasks can be resumed", 409)
        if self.active_worker(task_id):
            fail("TASK_BUSY", "wait for the previous worker to finish pausing", 409)
        if self.cancel_requested(row):
            fail("CANCELLATION_PENDING", "task cancellation is irrevocable; reconcile stop instead of resuming", 409)
        if row.harness_json is None:
            fail("LEGACY_TASK", "该任务创建于新版 harness 之前，请新建任务", 409)
        apply = settle(row, row.pending_decision_json) if settle else None
        if row.pending_decision_json and apply is None:
            fail("DECISION_PENDING", "approve or reject the pending decision before resuming", 409)
        self.settle_interrupted_usage(db, row)
        if row.context_json.get("paper_available") and not row.context_json.get("text"):
            context = self.paper_context(db, row.project_id, row.context_json.get("paper_version_id"))
            if not context.get("text"):
                fail("PAPER_CONTENT_REQUIRED", "finish verified paper parsing before resume", 409)
            row.context_json = {**row.context_json, **context}
        if apply:
            apply()
        self.remaining(row)
        self.resolve_models(row)
        row.state, row.error_json = "queued", None
        drop_auto_resume(row)
        self.event(db, row, "task_resumed", {"jobs_to_reconcile": row.jobs_json})
        return row

    def withdraw_decision(self, db, row, reason):
        """Drop a pending decision nobody can answer any more. Idempotent: the
        event is written only when a decision is actually withdrawn."""
        decision = row.pending_decision_json
        if not decision:
            return
        row.pending_decision_json = None
        self.event(db, row, "decision_withdrawn", {"decision_id": decision.get("id"), "reason": reason})

    def approve_decision(self, db, row, decision):
        # The user approves the precise frozen proposal, not merely a resume.
        row.protocol_json = decision["proposed"]
        row.pending_decision_json = None
        row.reference_sources_confirmed = False
        self.event(db, row, "protocol_approved", {"decision_id": decision["id"], "protocol": row.protocol_json})
        row.report_json = None

    def reject_decision(self, db, row, decision, note=None):
        # The current protocol stays in force; Pi learns of the rejection as a user message.
        row.pending_decision_json = None
        self.event(db, row, "protocol_rejected", {"decision_id": decision["id"],
            "proposed_sha256": decision["proposed"]["sha256"], "changed": decision.get("changed", []), "note": note})
        text = f"Protocol proposal {decision['id']} was rejected by the user; the current protocol stays in force."
        self.deliver_message(db, row, f"{text} Note: {note}" if note else text)

    def decide(self, db, task_id, decision_id, action, note=None):
        """Explicit answer to the pending decision; resumes the task under the resulting protocol."""
        if action not in ("approve", "reject"):
            fail("VALIDATION_ERROR", "decision action must be approve or reject")

        def settle(row, decision):
            if not decision or decision.get("id") != decision_id:
                fail("DECISION_MISMATCH", "decision_id does not match the pending decision", 409)
            if action == "approve":
                return lambda: self.approve_decision(db, row, decision)
            return lambda: self.reject_decision(db, row, decision, note)
        return self.resume(db, task_id, settle)

    def control(self, db, task_id, action, message=None):
        row = self.row(db, task_id)
        worker = self.active_worker(task_id)
        active = worker is not None
        if action == "message":
            if not message:
                fail("VALIDATION_ERROR", "message is required")
            self.deliver_message(db, row, message)
            return row
        if action == "resume":
            def settle(row, decision):
                # Legacy path: `approve:<pending_decision.id>` as the resume message.
                if not decision or not message:
                    return None  # with a pending decision, resume() reports DECISION_PENDING
                if message != f"approve:{decision['id']}":
                    fail("PROTOCOL_APPROVAL_REQUIRED", "resume message must be approve:<pending_decision.id>", 409)
                return lambda: self.approve_decision(db, row, decision)
            return self.resume(db, task_id, settle)
        if action == "pause":
            if row.state not in ("queued", "running", "paused"):
                fail("INVALID_STATE", "task cannot be paused", 409)
            row.state = "paused"
            drop_auto_resume(row)  # a person's pause wins over a scheduled automatic resume
            self.event(db, row, "task_paused", {"background_jobs_retained": True})
            if active:
                worker["bridge"].abort()
            return row
        if action == "stop":
            self.request_cancel(row)
            self.withdraw_decision(db, row, "task_stopped")
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
            # A turn still finishing when stop was requested may have left a proposal.
            self.withdraw_decision(db, row, "task_stopped")
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
