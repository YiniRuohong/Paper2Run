"""Turn driver: after a main-session turn, review a pending candidate or continue with facts only."""
from __future__ import annotations

import json

from . import evidence
from .candidate import gate
from .review import run_review

BUDGET_ERRORS = {"budget_exceeded", "wall_time_exceeded", "BUDGET_EXHAUSTED"}
ENDED_JOB_STATES = {"stopped", "cancelled", "canceled", "completed", "succeeded", "failed",
                    "timed_out", "timeout", "exited"}
NO_PROGRESS_LIMIT = 3
REVIEW_ATTEMPTS = 2


def max_repair_rounds(row):
    return int(row.config_snapshot["limits"].get("max_repair_rounds", 3))


def fingerprint(row):
    candidate = (row.harness_json or {}).get("candidate")
    ended = sum(1 for job in row.jobs_json.values() if job.get("state") in ENDED_JOB_STATES)
    return [len(row.artifacts_json), ended, candidate["snapshot_sha256"] if candidate else None]


def findings_text(review):
    lines = [f"Independent review of candidate {review['snapshot_sha256']} (round {review['round']}) "
             f"returned verdict {review['verdict']} with these findings:"]
    for index, finding in enumerate(review["findings"], 1):
        lines.append(f"{index}. {finding['description']}")
        lines += [f"   evidence: {item}" for item in finding["evidence"]]
    return "\n".join(lines)


def facts_message(service, db, row, findings=True):
    """Program-observed facts for a continuation. States facts only, never what to do."""
    harness = row.harness_json or {}
    round_ = evidence.current_round(row)
    limit = max_repair_rounds(row)
    artifacts = evidence.round_artifacts(row)
    lines = ["Program-observed task facts after the last turn.",
             f"Repair round: {round_} of {limit}; repair rounds left: {max(0, limit - round_)}.",
             f"Artifacts registered in round {round_}:"]
    lines += [f"- {a['name']} (kind {a['kind']}, round {a.get('round', round_)})" for a in artifacts] or ["- none"]
    lines.append("Jobs:")
    lines += [f"- {launch_id}: job_id {job.get('job_id')}, state {job.get('state', 'unknown')}, "
              f"exit_code {job.get('exit_code')}, command {json.dumps(str(job.get('command', ''))[:200])}"
              for launch_id, job in row.jobs_json.items()] or ["- none"]
    missing = gate(db, row)
    lines.append("submit_candidate requirements not yet met: " + ("; ".join(missing) if missing else "none") + ".")
    remaining = service.remaining(row)
    lines.append("Remaining budget: " + ", ".join(f"{key} {value}" for key, value in remaining.items()) + ".")
    reviews = harness.get("reviews") or []
    if reviews and findings:
        lines.append("Latest review findings, verbatim:")
        lines.append(findings_text(reviews[-1]))
    return service.profiles.clean("\n".join(lines), bounded=False)


def finish(service, db, row, outcome, reason=None):
    candidate = (row.harness_json or {}).get("candidate")
    evidence.report(service, db, row, narrative=candidate["summary"] if candidate else "",
                    outcome=outcome, reason=reason)
    row.outcome, row.state = outcome, "completed"
    payload = {"state": row.state, "outcome": outcome, "verdict": "not_comparable"}
    service.event(db, row, "task_finished", {**payload, "reason": reason} if reason else payload)


def budget_outcome(row):
    """Outcome when the budget runs out: (outcome, reason), or (None, None) to keep the lifecycle-only result."""
    harness = row.harness_json or {}
    candidate = harness.get("candidate")
    if harness.get("reviews"):
        return "completed_with_issues", "budget_exhausted"
    if candidate and candidate.get("review_status") == "pending":
        return "review_unavailable", "budget_exhausted"
    return None, None


class _ReviewHandle:
    """Registered as the task's bridge during review: pause/stop/shutdown abort it; user steering
    never reaches the independent reviewer (messages stay persisted for the main session)."""
    def __init__(self, bridge):
        self.bridge = bridge

    def abort(self):
        self.bridge.abort()

    def steer(self, message):
        pass


def _attempt(service, task_id, bridge_factory, model, account):
    """One review session. Returns (review, error); the main bridge is restored afterwards."""
    with service.lock:
        worker = service.workers.get(task_id)
        main = worker.get("bridge") if worker else None
    review, error = None, None
    try:
        bridge = bridge_factory()
        if worker is not None:
            with service.lock:
                worker["bridge"] = _ReviewHandle(bridge)
        review = run_review(service, task_id, bridge, model, account)
    except Exception as exc:  # every review failure is one failed attempt (budget excepted below)
        error = exc
    finally:
        if worker is not None:
            with service.lock:
                worker["bridge"] = main
    return review, error


def _review(service, task_id, bridge_factory, model, account):
    while True:
        review, error = _attempt(service, task_id, bridge_factory, model, account)
        if error is not None and getattr(error, "code", None) in BUDGET_ERRORS:
            raise error  # the worker records the budget cancellation; budget_outcome decides
        with service.lock, service.sessions() as db:
            row = service.row(db, task_id)
            if row.state != "running":
                # Paused, stopped or budget-cancelled during review: the candidate stays pending
                # and the same snapshot is reviewed on resume.
                return "finished", None
            harness = row.harness_json
            candidate = harness["candidate"]
            attempt = int(candidate.get("attempts", 0)) + 1
            if error is not None:
                reason = getattr(error, "reason", None) or f"{type(error).__name__}: {error}"
                candidate = {**candidate, "attempts": attempt}
                if attempt >= REVIEW_ATTEMPTS:
                    candidate["review_status"] = "unavailable"
                row.harness_json = {**harness, "candidate": candidate}
                service.event(db, row, "review_failed", {"snapshot_sha256": candidate["snapshot_sha256"],
                                                         "attempt": attempt, "reason": service.profiles.clean(reason)})
                if attempt >= REVIEW_ATTEMPTS:
                    finish(service, db, row, "review_unavailable", reason="review_failed")
                    db.commit()
                    return "finished", None
                db.commit()
                continue
            record = {"snapshot_sha256": candidate["snapshot_sha256"], "round": candidate["round"],
                      "attempt": attempt, "verdict": review["verdict"], "findings": review["findings"]}
            service.event(db, row, "review_recorded", {key: record[key] for key in
                                                       ("snapshot_sha256", "verdict", "findings", "attempt")})
            candidate = {**candidate, "review_status": review["verdict"]}
            harness = {**harness, "candidate": candidate, "reviews": [*harness.get("reviews", []), record]}
            row.harness_json = harness
            if review["verdict"] == "pass":
                finish(service, db, row, "verified")
                db.commit()
                return "finished", None
            if harness.get("repair_round", 0) >= max_repair_rounds(row):
                finish(service, db, row, "completed_with_issues")
                db.commit()
                return "finished", None
            row.harness_json = {**harness, "repair_round": harness.get("repair_round", 0) + 1, "no_progress": 0}
            row.harness_json = {**row.harness_json, "fingerprint": fingerprint(row)}
            prompt = findings_text(record) + "\n\n" + facts_message(service, db, row, findings=False)
            service.event(db, row, "continuation_sent", {"reason": "review_issues",
                                                         "repair_round": row.harness_json["repair_round"]})
            db.commit()
            return "continue", prompt


def after_turn(service, task_id, bridge_factory, model, account, narrative=""):
    """Decide what follows a turn: ("continue", prompt) or ("finished", None)."""
    with service.lock, service.sessions() as db:
        row = service.row(db, task_id)
        if row.state != "running" or row.harness_json is None:
            return "finished", None  # legacy tasks finish in the worker's result path
        harness = row.harness_json
        candidate = harness.get("candidate")
        pending = bool(candidate and candidate.get("review_status") == "pending")
        if not pending:
            current = fingerprint(row)
            no_progress = harness.get("no_progress", 0) + 1 if current == harness.get("fingerprint") else 0
            row.harness_json = {**harness, "fingerprint": current, "no_progress": no_progress}
            if no_progress >= NO_PROGRESS_LIMIT:
                evidence.report(service, db, row, narrative=narrative, outcome=None)
                row.state = "incomplete"
                service.event(db, row, "task_finished", {"state": row.state, "outcome": None,
                                                         "verdict": "not_comparable"})
                db.commit()
                return "finished", None
            prompt = facts_message(service, db, row)
            service.event(db, row, "continuation_sent", {"reason": "turn_ended", "no_progress": no_progress})
            db.commit()
            return "continue", prompt
    return _review(service, task_id, bridge_factory, model, account)
