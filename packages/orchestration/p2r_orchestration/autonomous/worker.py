"""One background thread per task; all persistent transitions use service lock."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time

from p2r_persistence.models import utcnow
from . import driver, evidence
from .candidate import freeze
from .driver import BUDGET_ERRORS
from .evidence import ensure_not_under_review
from .security import fail
from p2r_orchestration.service import ServiceError
from .tools import TOOLS, SYSTEM_PROMPT


def _arguments(name, args):
    schema = next((t["parameters"] for t in TOOLS if t["name"] == name), None)
    if not schema or not isinstance(args, dict) or set(args) - set(schema["properties"]) or set(schema["required"]) - set(args):
        raise ValueError("invalid tool or arguments")
    for key, value in args.items():
        rule = schema["properties"][key]
        kind = rule["type"]
        if ((kind == "string" and not isinstance(value, str)) or
            (kind == "integer" and type(value) is not int) or
            (kind == "object" and not isinstance(value, dict))):
            raise ValueError("invalid tool argument type")
        if kind == "string" and not rule.get("minLength", 0) <= len(value) <= rule.get("maxLength", 1000000):
            raise ValueError("invalid tool string length")
        if kind == "integer" and not rule.get("minimum", 0) <= value <= rule.get("maximum", 2**53):
            raise ValueError("invalid tool numeric range")
        if "enum" in rule and value not in rule["enum"]:
            raise ValueError("invalid tool option")
    return dict(args)


def dispatch(service, target, task_id, name, args, request_id):
    args = _arguments(name, args)
    service.profiles.reject_credentials(args)
    with service.lock, service.sessions() as db:
        row = service.row(db, task_id)
        if row.state != "running" or service.cancel_requested(row):
            fail("TASK_PAUSED", "task is not accepting tool execution", 409)
        remaining = service.remaining(row)
        deadline = time.monotonic() + remaining["wall_time_seconds"]
        service.event(db, row, "tool_started", {"id": request_id, "name": name, "arguments": args})
        if name in ("background", "execute"):
            launch_id = args.get("launch_id") or f"execute-{request_id}"
            if len(launch_id) > 256:
                raise ValueError("launch_id exceeds 256 characters")
            timeout = min(args.get("timeout_seconds", 60), max(1, int(remaining["wall_time_seconds"])))
            job_id = hashlib.sha256(launch_id.encode()).hexdigest()
            previous = row.jobs_json.get(launch_id)
            intent = {"job_id": job_id, "command": args["command"], "timeout_seconds": timeout, "state": "submitting",
                      "protocol_sha256": row.protocol_json["sha256"] if row.protocol_json else None,
                      "entrypoint": row.protocol_json["protocol"]["entrypoint"] if row.protocol_json else None,
                      "sequence": row.event_sequence}  # launch sequence; reused intents keep the original
            if previous:
                if previous["command"] != args["command"]:
                    fail("LAUNCH_CONFLICT", "launch_id belongs to a different command", 409)
                intent = previous
                timeout = intent["timeout_seconds"]
            else:
                row.jobs_json = {**row.jobs_json, launch_id: intent}
            # Commit intent before target call; lost responses reconcile by stable ID.
        if name in ("status", "logs", "stop") and args["job_id"] not in {j["job_id"] for j in row.jobs_json.values()}:
            fail("JOB_NOT_OWNED", "job does not belong to this task", 403)
        if name == "propose_protocol":
            ensure_not_under_review(row)
            result = service.propose(db, row, args["protocol"], args["reason"])
            db.commit()
            return result
        if name == "note_progress":
            service.event(db, row, "progress_noted", args)
            db.commit()
            return {"recorded": True}
        db.commit()
    if name in ("background", "execute"):
        result = target.start(args["command"], launch_id, timeout)
        with service.lock, service.sessions() as db:
            row = service.row(db, task_id)
            row.jobs_json = {**row.jobs_json, launch_id: {**intent, **service.profiles.clean(result)}}
            should_stop = service.cancel_requested(row) or row.state in ("stopping", "stopped")
            if should_stop:
                service.request_cancel(row)
                row.state = "stopping"
                service.event(db, row, "late_launch_cancelled", {"launch_id": launch_id, "job_id": result["job_id"]})
            db.commit()
        if should_stop:
            # Reconcile all owned handles again, even after an earlier unknown
            # stop transitioned the display state to paused.
            service.finish_stop(task_id)
            result = target.status(result["job_id"])
        if name == "execute":
            while result.get("state") in ("starting", "pending", "running", "submitted"):
                with service.lock, service.sessions() as db:
                    if service.row(db, task_id).state != "running":
                        return {**result, "decision_paused": True}
                if time.monotonic() >= deadline:
                    fail("BUDGET_EXHAUSTED", "cumulative task wall-time budget exhausted", 409)
                time.sleep(.1)
                result = target.status(result["job_id"])
            result = {**result, "logs": target.logs(result["job_id"], offset=0, limit=16000)}
        return result
    if name == "register_artifact":
        return evidence.register(service, target, task_id, args)
    if name == "submit_candidate":
        candidate = freeze(service, target, task_id, args["summary"])
        return {"status": "candidate_submitted", "snapshot_sha256": candidate["snapshot_sha256"],
                "round": candidate["round"],
                "next": "End this turn. Evaluation and an independent review run after the turn; findings, if any, arrive in the next message."}
    return getattr(target, name)(**args)


def observe_job(row, observation):
    """Record a job's latest observed state/exit code on its owned intent (launch facts unchanged)."""
    if not isinstance(observation, dict) or not observation.get("job_id"):
        return
    for launch_id, job in row.jobs_json.items():
        if job.get("job_id") == observation["job_id"]:
            observed = {k: observation[k] for k in ("state", "exit_code") if k in observation}
            if any(job.get(k) != v for k, v in observed.items()):
                row.jobs_json = {**row.jobs_json, launch_id: {**job, **observed}}
            return


def run_worker(service, task_id, bridge, message):
    target, baseline, started = None, None, time.monotonic()
    observed = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    pending_tokens = 0
    unverified_tokens = 0
    budget_stopped = False
    last_response = None
    turn_ended = False  # the main session delivered a terminal result for the current run

    def usage(row, incoming=None):
        if incoming:
            for key in observed:
                value = incoming.get(key, 0)
                if type(value) in (int, float) and value >= 0:
                    observed[key] = max(observed[key], int(value))
        if baseline is not None:
            row.usage_json = {**{key: baseline.get(key, 0) + value for key, value in observed.items()},
                "wall_time_seconds": baseline.get("wall_time_seconds", 0) + time.monotonic() - started,
                "active_since": utcnow().isoformat(), "pending_token_reservation": pending_tokens}
            # Failed calls with unknown usage remain charged conservatively even
            # when a later model call succeeds. Provider totals exclude these.
            row.usage_json["total_tokens"] += unverified_tokens
            reserved = baseline.get("unverified_token_reservation", 0) + unverified_tokens
            if reserved:
                row.usage_json["unverified_token_reservation"] = reserved

    def record_budget_cancel(db, row, error):
        nonlocal budget_stopped
        if error.get("code") not in BUDGET_ERRORS:
            return False
        if budget_stopped:
            return False
        budget_stopped = True
        reason = service.profiles.clean(error)
        service.request_cancel(row, reason)
        row.state, row.error_json = "stopping", reason
        service.event(db, row, "budget_exhausted", {"reason": reason, "usage": row.usage_json})
        return True

    def check_budget(db, row):
        try:
            service.remaining(row)
        except ServiceError as exc:
            if exc.code != "BUDGET_EXHAUSTED":
                raise
            return record_budget_cancel(db, row, {"code": exc.code, "message": str(exc)})
        return False

    def stop_for_budget():
        # Call only after the cancellation transaction commits and releases lock.
        bridge.abort()
        worker = service.workers.get(task_id) or {}
        if worker.get("bridge") not in (None, bridge):
            worker["bridge"].abort()  # a review session is running
        service.finish_stop(task_id)

    def callback(message, role="main"):
        # role="review": charge usage and budget only; never session state, narrative or outcome.
        nonlocal pending_tokens, unverified_tokens, last_response, turn_ended
        kind = message.get("type")
        review = role == "review"
        stop_budget = False
        if kind == "tool_request":
            request_id = str(message.get("id", ""))[:256]
            name = message.get("name")
            try:
                with service.lock, service.sessions() as db:
                    usage(service.row(db, task_id))
                    db.commit()
                result = dispatch(service, target, task_id, name, message.get("arguments"), request_id)
                is_error = False
            except Exception as exc:
                result, is_error = {"code": getattr(exc, "code", "tool_error"), "message": str(exc)}, True
            safe = service.profiles.clean(result)
            with service.lock, service.sessions() as db:
                row = service.row(db, task_id)
                usage(row)
                if is_error:
                    stop_budget = record_budget_cancel(db, row, result)
                elif name in ("execute", "status"):
                    observe_job(row, safe)
                if row.state == "running":
                    stop_budget = check_budget(db, row) or stop_budget
                service.event(db, row, "tool_completed", {"id": request_id, "name": name, "result": safe, "is_error": is_error})
                paused = row.state != "running"
                db.commit()
            if stop_budget:
                stop_for_budget()
            elif paused:
                bridge.abort()
            return {"type": "tool_result", "id": request_id, "result": safe, "is_error": is_error}
        with service.lock, service.sessions() as db:
            row = service.row(db, task_id)
            if kind == "ready" and review:
                pass  # the review session lives under task_root/review; row.session_file stays the main one
            elif kind == "ready":
                path = Path(message["session_file"]).resolve()
                if not path.is_relative_to((service.task_root(task_id) / "session").resolve()):
                    raise ValueError("SDK session path escaped task state")
                row.session_file = str(path)
                service.event(db, row, "session_ready", {"session_id": message.get("session_id")})
            elif kind == "event":
                event = message.get("event", {})
                sdk_message = event.get("message") or {}
                if event.get("type") == "turn_start":
                    # Count failed/aborted provider attempts as well as completed
                    # assistant messages; retries cannot reset the call budget.
                    observed["calls"] += 1
                    # If the process dies before provider usage arrives, reserve
                    # conservatively so repeated interruption cannot erase cost.
                    profiles = [row.config_snapshot["model_profile"], *row.config_snapshot.get("specialist_profiles", {}).values()]
                    reservation = max(p.get("context_window", 128000) + p.get("max_tokens", 4096) for p in profiles)
                    remaining_tokens = row.config_snapshot["limits"]["max_model_tokens"] - row.usage_json.get("total_tokens", 0)
                    pending_tokens = max(0, min(remaining_tokens, reservation))
                if event.get("type") == "message_end" and sdk_message.get("role") == "assistant":
                    content = sdk_message.get("content", [])
                    text = content if isinstance(content, str) else "\n".join(
                        part.get("text", "") for part in content
                        if isinstance(part, dict) and part.get("type") == "text")
                    if text and not event.get("delegated") and not review:
                        last_response = service.profiles.clean(text)
                    values = sdk_message.get("usage") or {}
                    if any(values.get(k, 0) for k in ("input", "output", "totalTokens", "cacheRead", "cacheWrite")):
                        pending_tokens = 0
                    elif event.get("delegated") or sdk_message.get("stopReason") in ("error", "aborted"):
                        unverified_tokens += pending_tokens
                        pending_tokens = 0
                    observed["input_tokens"] += sum(values.get(k, 0) or 0 for k in ("input", "cacheRead", "cacheWrite"))
                    observed["output_tokens"] += values.get("output", 0) or 0
                    observed["total_tokens"] += values.get("totalTokens", sum(values.get(k, 0) or 0 for k in ("input", "cacheRead", "cacheWrite", "output"))) or 0
                # Store bounded, redacted SDK observations, never raw stream deltas.
                if event.get("type") != "message_update":
                    service.event(db, row, "sdk_event", {**event, "session_role": "review"} if review else event)
            elif kind == "result":
                if isinstance(message.get("text"), str) and message["text"] and not review:
                    last_response = service.profiles.clean(message["text"])
                pending_tokens = 0
                usage(row, message.get("usage") or {})
                if row.state == "running":
                    stop_budget = check_budget(db, row)
                if review:
                    pass  # the review verdict comes only from submit_review
                elif row.harness_json is not None:
                    # The program decides what follows a turn (driver.after_turn), never the result text.
                    turn_ended = True
                    driver.mark_user_messages_delivered(db, row)  # steered into the live turn
                    service.event(db, row, "turn_ended", {"state": row.state})
                elif row.state == "running":
                    # Legacy task (pre-harness): keep the original result -> report path.
                    result = evidence.report(service, db, row, message.get("text", ""))
                    row.state = "incomplete" if result["status"] == "incomplete" else "completed"
                    service.event(db, row, "task_finished", {"state": row.state, "verdict": "not_comparable"})
            elif kind == "error":
                usage(row)
                error = {"code": message.get("code", "sdk_error"), "message": message.get("message", "SDK error")}
                stop_budget = record_budget_cancel(db, row, error)
                if review:
                    pass  # a non-budget review error is one failed review attempt (driver), not a pause
                elif row.state == "running":
                    row.state = "paused"
                    row.error_json = service.profiles.clean(error)
                    service.event(db, row, "task_error", row.error_json)
            usage(row)
            # Let an admitted provider call finish; enforce its actual usage
            # before any subsequent tool or model scheduling.
            if (kind == "event" and message.get("event", {}).get("type") == "message_end"
                    and row.state == "running"):
                stop_budget = check_budget(db, row) or stop_budget
            db.commit()
        if stop_budget:
            stop_for_budget()
        return None

    try:
        with service.lock, service.sessions() as db:
            row = service.row(db, task_id)
            if row.state != "queued" or service.closing:
                return
            limits = service.remaining(row)
            model, specialists = service.resolve_models(row)
            baseline = dict(row.usage_json)
            row.state, row.error_json = "running", None
            usage(row)
            service.event(db, row, "worker_started", {"resuming": bool(row.session_file), "remaining": limits})
            task_root = service.task_root(task_id)
            session_root = task_root / "session"
            session_root.mkdir(parents=True, exist_ok=True)
            target = service.make_target(row)
            service.workers[task_id]["target"] = target
            config = {"cwd": str(session_root), "session_dir": str(session_root), "model": model,
                      "system_prompt": SYSTEM_PROMPT, "tools": TOOLS, "limits": limits}
            if specialists:
                config["specialists"] = specialists
            if row.session_file:
                if not Path(row.session_file).is_file():
                    fail("SESSION_MISSING", "saved SDK session is missing; cannot safely resume", 409)
                config["session_file"] = row.session_file
            opening = {"objective": row.objective,
                "max_repair_rounds": row.config_snapshot["limits"].get("max_repair_rounds", 3),
                "paper": row.context_json, "protocol": row.protocol_json,
                "skills": [{"id": s["id"], "version": s["version"], "sha256": s["sha256"], "content": s["content"]} for s in row.skills_json],
                "existing_jobs": row.jobs_json, "instruction": message or "Continue the authorized research task; reconcile existing jobs first."}
            prompt = json.dumps(service.profiles.clean(opening, bounded=False), ensure_ascii=False)
            candidate = (row.harness_json or {}).get("candidate")
            review_pending = bool(candidate and candidate.get("review_status") == "pending")
            driver.mark_user_messages_delivered(db, row)  # the opening carries context_json.user_messages
            db.commit()
        for action in ("probe", "prepare"):
            with service.lock, service.sessions() as db:
                if service.row(db, task_id).state != "running":
                    return
            observation = getattr(target, action)()
            with service.lock, service.sessions() as db:
                row = service.row(db, task_id)
                usage(row)
                service.event(db, row, f"target_{action}", observation)
                stop_budget = check_budget(db, row) if row.state == "running" else False
                db.commit()
            if stop_budget:
                stop_for_budget()
                return
        # Reconcile handles before the SDK receives further decisions.
        with service.lock, service.sessions() as db:
            jobs = dict(service.row(db, task_id).jobs_json)
        for launch_id, job in jobs.items():
            try:
                observation = target.status(job["job_id"])
            except Exception as exc:
                observation = {"state": "unknown", "error": str(exc)}
            with service.lock, service.sessions() as db:
                row = service.row(db, task_id)
                usage(row)
                service.event(db, row, "job_reconciled", {"launch_id": launch_id, "observation": observation})
                observe_job(row, service.profiles.clean(observation))
                stop_budget = check_budget(db, row) if row.state == "running" else False
                continue_reconciling = row.state == "running"
                db.commit()
            if stop_budget:
                stop_for_budget()
                return
            if not continue_reconciling:
                return

        def review_account(sdk_message):
            return callback(sdk_message, role="review")

        def next_turn():
            # Program-decided follow-up of a turn: review a pending candidate, or continue with facts.
            # A run that returns without a result is not a turn end; it pauses for explicit resume.
            if not (turn_ended or review_pending):
                return None
            decision, follow = driver.after_turn(service, task_id, service.bridge_factory, model,
                                                 review_account, narrative=last_response or "")
            if decision == "finished":
                return None
            with service.lock, service.sessions() as db:
                session_file = service.row(db, task_id).session_file
            if session_file:
                config["session_file"] = session_file
                return follow
            # No main session yet: the normal opening prompt plus the follow-up facts/findings.
            return json.dumps(service.profiles.clean({**opening, "continuation": follow}, bounded=False),
                              ensure_ascii=False)

        if review_pending:
            # Resume: finish the interrupted review of the same snapshot before the main session.
            prompt = next_turn()
            review_pending = False
        while prompt is not None:
            with service.lock, service.sessions() as db:
                row = service.row(db, task_id)
                if row.state != "running":
                    return
                usage(row)
                config["limits"] = service.remaining(row)
                # Messages that arrived while no main process could take a steer (review, between turns).
                prompt = driver.with_user_messages(db, row, prompt)
                db.commit()
            turn_ended = False
            bridge.run(config, prompt, callback)
            prompt = next_turn()
    except Exception as exc:
        with service.lock, service.sessions() as db:
            row = service.row(db, task_id)
            usage(row)
            error = {"code": getattr(exc, "code", "worker_error"), "message": str(exc)}
            stop_budget = record_budget_cancel(db, row, error)
            if row.state == "running":
                stop_budget = check_budget(db, row) or stop_budget
            if row.state in ("queued", "running"):
                row.state = "paused"
                row.error_json = service.profiles.clean(error)
                service.event(db, row, "task_error", row.error_json)
            db.commit()
        if stop_budget:
            stop_for_budget()
    finally:
        if target:
            target.close()  # Application exit/pause must not kill background jobs.
        with service.lock, service.sessions() as db:
            row = service.row(db, task_id)
            usage(row)
            settled = {k: v for k, v in row.usage_json.items() if k not in ("active_since", "pending_token_reservation")}
            if pending_tokens:
                settled["total_tokens"] = settled.get("total_tokens", 0) + pending_tokens
                settled["unverified_token_reservation"] = settled.get("unverified_token_reservation", 0) + pending_tokens
            row.usage_json = settled
            try:
                outcome, reason, snapshot = (driver.budget_outcome(row) if budget_stopped and row.outcome is None
                                             else (None, None, None))
                if outcome:
                    # A submitted candidate decides the outcome; the lifecycle state stays the budget stop.
                    candidate = row.harness_json["candidate"]
                    if candidate.get("review_status") == "pending":
                        candidate = {**candidate, "review_status": "unavailable"}
                    row.harness_json = {**row.harness_json, "candidate": candidate, "outcome_snapshot_sha256": snapshot}
                    evidence.report(service, db, row, candidate["summary"], outcome=outcome, reason=reason,
                                    outcome_snapshot=snapshot)
                    row.outcome = outcome
                    service.event(db, row, "task_finished", {"state": row.state, "outcome": outcome,
                        "verdict": "not_comparable", "reason": reason, "outcome_snapshot_sha256": snapshot})
                elif budget_stopped and row.outcome is None:
                    # Budget cancellation forbids new work, not the report for the
                    # admitted response. The SDK may emit aborted instead of result.
                    narrative = last_response if last_response is not None else (row.report_json or {}).get("model_narrative", "")
                    evidence.report(service, db, row, narrative)
            except Exception as exc:
                # Settlement and worker_exited below must always commit.
                service.event(db, row, "report_failed", {"code": getattr(exc, "code", "report_error"), "message": str(exc)})
            if row.state == "running":
                row.state = "paused"
                row.error_json = {"code": "recovery_required", "message": "SDK stopped; explicit resume required."}
            service.event(db, row, "worker_exited", {"state": row.state,
                "background_jobs_retained": row.state != "stopped"})
            db.commit()
