"""Turn driver: review after a turn, repair rounds, continuation and outcomes (scripted bridges)."""
from pathlib import Path

import pytest
from sqlalchemy import select

from p2r_orchestration.autonomous import driver
from p2r_persistence.autonomous import AutonomousEventRecord as Event

from .test_service import Bridge, Target, create, protocol, service, wait  # noqa: F401

FILES = [("env.json", "environment", "{}"), ("data.json", "data", "{}"),
         ("predictions.json", "result", '[{"id":"a","prediction":1}]'),
         ("labels.json", "result", '[{"id":"a","label":1}]')]
FINDING = {"description": "labels.json holds 1 row; the protocol split requires the full test split.",
           "evidence": ["labels.json"]}
ADVICE = ("you should", "next step", "建议")


def work(tag="r0"):
    """Tool calls for one complete candidate: code, a formal run after it, remaining evidence, submit."""
    calls = [("write", {"path": "main.py", "content": f"print('{tag}')"}),
             ("register_artifact", {"path": "main.py", "name": "main.py", "kind": "code"}),
             ("execute", {"command": "python3 main.py", "timeout_seconds": 5})]
    for name, kind, content in FILES:
        calls += [("write", {"path": name, "content": content}),
                  ("register_artifact", {"path": name, "name": name, "kind": kind})]
    return calls + [("submit_candidate", {"summary": f"candidate {tag}"})]


class Main(Bridge):
    """Main session: one scripted list of tool calls per turn, then a result."""
    def __init__(self, turns=()):
        super().__init__()
        self.turns, self.prompts, self.configs, self.responses = list(turns), [], [], []

    def run(self, config, prompt, callback):
        self.prompts.append(prompt)
        self.configs.append(dict(config))
        turn = len(self.prompts)
        path = Path(config["session_dir"]) / "main.jsonl"
        path.write_text('{"type":"session"}\n')
        callback({"type": "ready", "session_file": str(path), "session_id": "main"})
        for index, (name, arguments) in enumerate(self.turns.pop(0) if self.turns else []):
            self.responses.append(callback({"type": "tool_request", "id": f"{turn}-{index}",
                                            "name": name, "arguments": arguments}))
        callback({"type": "result", "text": f"turn {turn}", "usage": {}})


class Review(Bridge):
    """Review session: optionally submits one verdict; on_run injects side effects."""
    def __init__(self, verdict=None, findings=(), on_run=None, messages=()):
        super().__init__()
        self.verdict, self.findings, self.on_run, self.messages = verdict, list(findings), on_run, messages
        self.prompt = self.config = None

    def run(self, config, prompt, callback):
        self.config, self.prompt = config, prompt
        callback({"type": "ready", "session_file": config["session_dir"] + "/review.jsonl", "session_id": "review"})
        for message in self.messages:
            callback(message)
        if self.on_run:
            self.on_run()
        if self.verdict:
            callback({"type": "tool_request", "id": "verdict", "name": "submit_review",
                      "arguments": {"verdict": self.verdict, "findings": self.findings}})
        callback({"type": "result", "text": "reviewed", "usage": {}})


class Crashing(Bridge):
    def run(self, config, prompt, callback):
        raise RuntimeError("node binary missing")


def prepare(service, bridges, rounds=None):
    task_id = create(service, protocol=protocol())
    if rounds is not None:
        with service.sessions() as db:
            row = service.row(db, task_id)
            row.config_snapshot = {**row.config_snapshot,
                                   "limits": {**row.config_snapshot["limits"], "max_repair_rounds": rounds}}
            db.commit()
    queue = list(bridges)
    service.bridge_factory = lambda: queue.pop(0)
    return task_id


def start(service, bridges, rounds=None):
    task_id = prepare(service, bridges, rounds)
    service.launch(task_id)
    return task_id, wait(service, task_id)


def events(service, task_id, kind):
    with service.sessions() as db:
        return [e.payload for e in db.scalars(select(Event).where(Event.task_id == task_id, Event.type == kind)
                                                 .order_by(Event.sequence)).all()]


def test_pass_gives_verified(service):
    main = Main([work()])
    task_id, row = start(service, [main, Review("pass")])
    assert row.outcome == "verified" and row.state == "completed"
    assert row.report_json["outcome"] == "verified" and row.report_json["verdict"] == "not_comparable"
    assert row.report_json["candidate"]["snapshot_sha256"] == row.harness_json["candidate"]["snapshot_sha256"]
    assert row.report_json["evaluation"]["metrics"]["mae"] == 0
    assert row.report_json["model_narrative"] == "candidate r0"
    assert len(main.prompts) == 1
    recorded = events(service, task_id, "review_recorded")
    assert recorded == [{"snapshot_sha256": row.harness_json["candidate"]["snapshot_sha256"],
                         "verdict": "pass", "findings": [], "attempt": 1}]
    assert events(service, task_id, "task_finished")[-1] == {"state": "completed", "outcome": "verified",
                                                            "verdict": "not_comparable"}
    assert events(service, task_id, "turn_ended")


def test_issues_then_repair_then_verified(service):
    main = Main([work("r0"), work("r1")])
    task_id, row = start(service, [main, Review("issues", [FINDING]), Review("pass")])
    assert len(main.prompts) == 2
    assert FINDING["description"] in main.prompts[1]
    assert main.configs[1]["session_file"] == row.session_file
    assert row.harness_json["repair_round"] == 1
    assert row.harness_json["candidate"]["round"] == 1
    assert [r["verdict"] for r in row.harness_json["reviews"]] == ["issues", "pass"]
    assert row.outcome == "verified" and row.state == "completed"
    assert all(not r.get("is_error") for r in main.responses if r)


def test_rounds_exhausted_gives_completed_with_issues(service):
    main = Main([work("r0"), work("r1")])
    task_id, row = start(service, [main, Review("issues", [FINDING]), Review("issues", [FINDING])], rounds=1)
    assert row.outcome == "completed_with_issues" and row.state == "completed"
    assert row.harness_json["repair_round"] == 1 and len(main.prompts) == 2
    assert row.harness_json["candidate"]["review_status"] == "issues"
    assert row.report_json["outcome"] == "completed_with_issues"
    assert len(row.report_json["reviews"]) == 2


def test_zero_repair_rounds_finishes_on_first_issues(service):
    main = Main([work()])
    task_id, row = start(service, [main, Review("issues", [FINDING])], rounds=0)
    assert row.outcome == "completed_with_issues" and row.state == "completed"
    assert row.harness_json["repair_round"] == 0 and len(main.prompts) == 1


def test_review_failure_retries_once_then_unavailable(service):
    main = Main([work()])
    task_id, row = start(service, [main, Review(None), Crashing()])
    assert row.outcome == "review_unavailable" and row.state == "completed"
    assert row.harness_json["candidate"]["attempts"] == 2
    failed = events(service, task_id, "review_failed")
    assert [f["attempt"] for f in failed] == [1, 2] and "node binary missing" in failed[1]["reason"]
    assert not events(service, task_id, "review_recorded") and len(main.prompts) == 1
    review_root = service.task_root(task_id) / "review" / row.harness_json["candidate"]["snapshot_sha256"]
    assert (review_root / "attempt-1").is_dir() and (review_root / "attempt-2").is_dir()


def test_no_progress_three_turns_incomplete(service):
    main = Main()
    task_id, row = start(service, [main])
    assert row.state == "incomplete" and row.outcome is None
    assert len(main.prompts) == 4 and row.harness_json["no_progress"] == 3
    assert row.report_json["outcome"] is None and row.report_json["verdict"] == "not_comparable"
    assert all(config["session_file"] == row.session_file for config in main.configs[1:])
    assert events(service, task_id, "task_finished")[-1]["state"] == "incomplete"


def test_facts_message_has_no_advice(service):
    main = Main([[("write", {"path": "main.py", "content": "print(1)"}),
                  ("register_artifact", {"path": "main.py", "name": "main.py", "kind": "code"})]])
    task_id, row = start(service, [main])
    continuations = main.prompts[1:]
    assert len(continuations) == 3
    first = continuations[0]
    for fact in ("main.py", "code", "environment artifact", "successful entrypoint run after latest code registration",
                 "max_model_calls", "repair rounds left: 3"):
        assert fact in first, fact
    with service.sessions() as db:
        direct = driver.facts_message(service, db, service.row(db, task_id))
    for message in [*continuations, direct]:
        assert not any(phrase in message.lower() for phrase in ADVICE), message


def test_facts_message_lists_jobs_and_latest_findings(service):
    main = Main([work("r0"), []])
    task_id, row = start(service, [main, Review("issues", [FINDING]), ], rounds=1)
    with service.sessions() as db:
        message = driver.facts_message(service, db, service.row(db, task_id))
    assert FINDING["description"] in message and "labels.json" in message
    assert "python3 main.py" in message and "completed" in message
    assert "repair rounds left: 0" in message


@pytest.mark.parametrize("reviewed", [False, True])
def test_budget_exhausted_during_review_gives_review_unavailable(service, reviewed):
    exhausted = Review(messages=({"type": "error", "code": "budget_exceeded", "message": "budget exhausted"},))
    turns = [work("r0"), work("r1")] if reviewed else [work("r0")]
    reviews = [Review("issues", [FINDING]), exhausted] if reviewed else [exhausted]
    main = Main(turns)
    task_id, row = start(service, [main, *reviews])
    assert row.state == "stopped" and service.cancel_requested(row)
    assert row.harness_json["candidate"]["attempts"] == 0
    assert not events(service, task_id, "review_failed")
    if reviewed:
        assert row.outcome == "completed_with_issues"
        assert row.report_json["outcome_reason"] == "budget_exhausted"
    else:
        assert row.outcome == "review_unavailable"
        assert row.report_json["outcome"] == "review_unavailable"
        assert row.report_json["outcome_reason"] == "budget_exhausted"
    assert events(service, task_id, "task_finished")[-1]["reason"] == "budget_exhausted"


def test_resume_with_pending_review_reruns_same_snapshot(service):
    holder = {}

    def pause():
        with service.lock, service.sessions() as db:
            service.control(db, holder["task_id"], "pause")
            db.commit()
    first_review, second_main, second_review = Review(on_run=pause), Main(), Review("pass")
    task_id = prepare(service, [Main([work()]), first_review, second_main, second_review])
    holder["task_id"] = task_id
    service.launch(task_id)
    row = wait(service, task_id)
    assert row.state == "paused" and row.outcome is None and first_review.aborted
    assert row.harness_json["candidate"]["review_status"] == "pending"
    assert row.harness_json["candidate"]["attempts"] == 0
    snapshot = row.harness_json["candidate"]["snapshot_sha256"]
    with service.lock, service.sessions() as db:
        service.control(db, task_id, "resume")
        db.commit()
    service.launch(task_id)
    row = wait(service, task_id)
    assert row.outcome == "verified" and row.state == "completed"
    assert second_main.prompts == []
    assert snapshot in second_review.prompt and row.harness_json["repair_round"] == 0
    assert len(events(service, task_id, "candidate_frozen")) == 1
    assert [r["snapshot_sha256"] for r in events(service, task_id, "review_recorded")] == [snapshot]
    assert not events(service, task_id, "review_failed")


def test_review_usage_charged_without_touching_main_session(service):
    usage = ({"type": "event", "event": {"type": "turn_start"}},
             {"type": "event", "event": {"type": "message_end", "message": {
                 "role": "assistant", "content": [{"type": "text", "text": "review text"}],
                 "usage": {"input": 30, "output": 20, "totalTokens": 50}}}})
    main = Main([work()])
    task_id, row = start(service, [main, Review("pass", messages=usage)])
    assert row.outcome == "verified"
    assert row.usage_json["calls"] == 1 and row.usage_json["total_tokens"] == 50
    assert row.session_file == main.configs[0]["session_dir"] + "/main.jsonl"
    assert len(events(service, task_id, "session_ready")) == 1
    assert row.report_json["model_narrative"] == "candidate r0"


def test_main_bridge_restored_after_review(service):
    main = Main([work()])
    task_id, row = start(service, [main, Review("pass")])
    assert service.workers[task_id]["bridge"] is main


def test_legacy_task_keeps_result_report_path(service):
    main = Main()
    task_id = prepare(service, [main])
    with service.sessions() as db:
        row = service.row(db, task_id)
        row.harness_json = None
        db.commit()
    service.launch(task_id)
    row = wait(service, task_id)
    assert row.state == "incomplete" and row.outcome is None and len(main.prompts) == 1
    assert row.report_json["model_narrative"] == "turn 1"
    assert not events(service, task_id, "turn_ended")


def test_fingerprint_counts_artifacts_ended_jobs_and_candidate(service):
    main = Main([work()])
    task_id, row = start(service, [main, Review("pass")])
    assert driver.fingerprint(row) == [len(row.artifacts_json), 1, row.harness_json["candidate"]["snapshot_sha256"]]
