import json
import os
from pathlib import Path

import pytest

from p2r_orchestration.autonomous import evidence
from p2r_orchestration.autonomous.review import (REVIEW_PROMPT, REVIEW_TOOLS, FrozenTarget, ReviewFailed,
                                                  run_review)
from p2r_orchestration.service import ServiceError

from .test_candidate import complete, running, submit
from .test_service import service  # noqa: F401


def frozen(service):
    task_id, target = running(service)
    complete(service, target, task_id)
    submit(service, target, task_id)
    with service.sessions() as db:
        snap = service.row(db, task_id).harness_json["candidate"]
    root = service.task_root(task_id) / "candidates" / snap["snapshot_sha256"]
    return task_id, target, snap, root


def code_of(caught):
    return caught.value.code


def test_frozen_target_rejects_escape(service, tmp_path):
    _, _, snap, root = frozen(service)
    view = FrozenTarget(root, snap)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret host file")
    os.symlink(outside, root / "artifacts" / "link.txt")
    for path in ("../x", "/etc/passwd", "link.txt", "../logs/" + snap["run"]["job_id"] + ".txt"):
        with pytest.raises(ServiceError) as caught:
            view.read(path)
        assert code_of(caught) == "INVALID_PATH", path


def test_frozen_target_rejects_symlinked_directory(service, tmp_path):
    _, _, snap, root = frozen(service)
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "f.txt").write_text("x")
    os.symlink(tmp_path / "elsewhere", root / "artifacts" / "dir")
    with pytest.raises(ServiceError) as caught:
        FrozenTarget(root, snap).read("dir/f.txt")
    assert code_of(caught) == "INVALID_PATH"


def test_frozen_target_ignores_live_changes(service):
    task_id, target, snap, root = frozen(service)
    target.files["main.py"] = b"changed after freeze"
    with service.sessions() as db:
        live = evidence.artifact_root(service, service.row(db, task_id))
    (live / "main.py").write_text("live store overwritten after freeze")
    view = FrozenTarget(root, snap)
    assert view.read("main.py")["content"] == "content of main.py"
    moved = {**snap, "artifacts": [{**a, "source_path": "/work/" + a["name"]} for a in snap["artifacts"]]}
    assert FrozenTarget(root, moved).read("/work/main.py")["content"] == "content of main.py"
    page = view.read("main.py", offset=8, limit=4)
    assert page["content"] == "of m" and page["next_offset"] == 12 and page["eof"] is False


def test_logs_only_for_candidate_job(service):
    _, _, snap, root = frozen(service)
    view = FrozenTarget(root, snap)
    job = snap["run"]["job_id"]
    assert view.logs(job)["stdout"].startswith("observed")
    assert view.status(job) == {**snap["run"], "state": "completed"} and "sequence" in view.status(job)
    for call in (view.logs, view.status):
        with pytest.raises(ServiceError) as caught:
            call("0" * 64)
        assert code_of(caught) == "JOB_NOT_OWNED" and caught.value.status == 403


class Scripted:
    """Bridge test double: sends scripted tool requests, records responses."""
    def __init__(self, script, tail=({"type": "result", "text": "done", "usage": {"total_tokens": 3}},)):
        self.script, self.tail = script, tail
        self.responses, self.config, self.prompt = [], None, None

    def run(self, config, prompt, callback):
        self.config, self.prompt = config, prompt
        callback({"type": "ready", "session_file": config["session_dir"] + "/s.jsonl", "session_id": "r"})
        for index, (name, arguments) in enumerate(self.script):
            self.responses.append(callback({"type": "tool_request", "id": f"t{index}", "name": name,
                                            "arguments": arguments}))
        for message in self.tail:
            callback(message)


def review(service, task_id, bridge):
    seen = []
    with service.sessions() as db:
        model = service.resolve_models(service.row(db, task_id))[0]
    return run_review(service, task_id, bridge, model, seen.append), seen


def test_run_review_returns_verdict(service):
    task_id, _, snap, _ = frozen(service)
    bridge = Scripted([("read", {"path": "main.py"}), ("submit_review", {"verdict": "pass", "findings": []})])
    result, seen = review(service, task_id, bridge)
    assert result == {"verdict": "pass", "findings": [], "snapshot_sha256": snap["snapshot_sha256"]}
    assert bridge.responses[0]["is_error"] is False
    assert bridge.responses[0]["result"]["content"] == "content of main.py"
    assert bridge.responses[1] == {"type": "tool_result", "id": "t1", "result": {"recorded": True}, "is_error": False}
    assert [m["type"] for m in seen] == ["ready", "result"]
    config = bridge.config
    session = Path(config["session_dir"])
    assert session.parent == service.task_root(task_id) / "review" / snap["snapshot_sha256"]
    assert session.name.startswith("attempt-1-") and len(session.name) == len("attempt-1-") + 8
    assert config["cwd"] == str(session)
    assert "specialists" not in config and "session_file" not in config
    assert config["system_prompt"] == REVIEW_PROMPT and config["tools"] == REVIEW_TOOLS
    with service.sessions() as db:
        assert config["limits"] == service.remaining(service.row(db, task_id))
    payload = json.loads(bridge.prompt)
    assert payload["protocol"]["sha256"] == snap["protocol_sha256"]
    assert {a["name"] for a in payload["candidate"]["artifacts"]} == {a["name"] for a in snap["artifacts"]}
    assert payload["evaluation"]["verdict"] == "not_comparable"
    assert payload["evaluation"]["comparison_eligible"] is False
    assert "paper" in payload and "unit-secret-123456" not in bridge.prompt


def test_issues_requires_finding(service):
    task_id, *_ = frozen(service)
    bridge = Scripted([("submit_review", {"verdict": "issues", "findings": []}),
                       ("submit_review", {"verdict": "issues", "findings": [{"description": "x", "evidence": []}]})])
    with pytest.raises(ReviewFailed) as caught:
        review(service, task_id, bridge)
    assert all(r["is_error"] for r in bridge.responses)
    assert "submit_review" in caught.value.reason


def test_issues_with_evidence_is_returned(service):
    task_id, _, snap, _ = frozen(service)
    finding = {"description": "metric differs from protocol", "evidence": ["predictions.json"]}
    bridge = Scripted([("submit_review", {"verdict": "issues", "findings": [finding]})])
    assert review(service, task_id, bridge)[0] == {"verdict": "issues", "findings": [finding],
                                                   "snapshot_sha256": snap["snapshot_sha256"]}


def test_long_findings_survive_intact(service):
    task_id, _, snap, _ = frozen(service)
    findings = [{"description": f"发现{i}：" + "结果与协议不一致" * 490, "evidence": ["predictions.json", "日志" * 400]}
                for i in range(20)]
    assert all(len(f["description"]) <= 4000 for f in findings)
    bridge = Scripted([("submit_review", {"verdict": "issues", "findings": findings})])
    result = review(service, task_id, bridge)[0]
    assert result == {"verdict": "issues", "findings": findings, "snapshot_sha256": snap["snapshot_sha256"]}


@pytest.mark.parametrize("arguments", [
    {"verdict": "maybe", "findings": []},
    {"verdict": "pass"},
    {"verdict": "pass", "findings": [], "extra": 1},
    {"verdict": "pass", "findings": [{"description": "", "evidence": []}]},
    {"verdict": "pass", "findings": [{"description": "x" * 4001, "evidence": []}]},
    {"verdict": "pass", "findings": [{"description": "x", "evidence": [1]}]},
    {"verdict": "pass", "findings": [{"description": "x", "evidence": [], "note": "y"}]},
    {"verdict": "pass", "findings": [{"description": "x", "evidence": []}] * 51},
])
def test_malformed_verdict_fails(service, arguments):
    task_id, *_ = frozen(service)
    bridge = Scripted([("submit_review", arguments)])
    with pytest.raises(ReviewFailed):
        review(service, task_id, bridge)
    assert bridge.responses[0]["is_error"] is True


def test_error_message_fails_review(service):
    task_id, *_ = frozen(service)
    bridge = Scripted([("submit_review", {"verdict": "pass", "findings": []})],
                      tail=({"type": "error", "code": "provider_error", "message": "boom"},))
    with pytest.raises(ReviewFailed) as caught:
        review(service, task_id, bridge)
    assert "provider_error" in caught.value.reason


def test_unknown_and_bad_tool_calls_are_tool_errors(service):
    task_id, *_ = frozen(service)
    bridge = Scripted([("write", {"path": "x", "content": "y"}), ("read", {"path": 3}),
                       ("logs", {"job_id": "0" * 64}), ("read", {"path": "../x"}),
                       ("submit_review", {"verdict": "pass", "findings": []})])
    assert review(service, task_id, bridge)[0]["verdict"] == "pass"
    assert [r["is_error"] for r in bridge.responses] == [True, True, True, True, False]
    assert bridge.responses[2]["result"]["code"] == "JOB_NOT_OWNED"
    assert bridge.responses[3]["result"]["code"] == "INVALID_PATH"


def test_review_has_no_mutating_tools():
    assert {t["name"] for t in REVIEW_TOOLS} == {"read", "logs", "status", "submit_review"}
    assert "evidence" in REVIEW_PROMPT and "issues" in REVIEW_PROMPT


def test_evaluation_at_evaluates_given_root(service):
    task_id, target, snap, root = frozen(service)
    other = root.parent / "elsewhere"
    other.mkdir()
    (other / "predictions.json").write_text('[{"id":"1","prediction":3}]')
    (other / "labels.json").write_text('[{"id":"1","label":2}]')
    custody = "Independent evaluation custody has not been verified by the control plane."
    with service.sessions() as db:
        row = service.row(db, task_id)
        result = evidence.evaluation_at(service, row, other)
        assert result["metrics"]["mae"] == 1.0
        assert result["verdict"] == "not_comparable" and result["comparison_eligible"] is False
        assert custody in result["reasons"] and "within_tolerance" not in result
        with pytest.raises(ValueError):  # live round holds non-JSON predictions
            evidence.evaluation(service, row)


def test_each_review_session_gets_a_fresh_directory(service):
    task_id, *_ = frozen(service)
    first, second = Scripted([]), Scripted([("submit_review", {"verdict": "pass", "findings": []})])
    with pytest.raises(ReviewFailed):
        review(service, task_id, first)
    review(service, task_id, second)
    assert first.config["session_dir"] != second.config["session_dir"]


def test_user_steering_never_reaches_reviewer(service):
    task_id, *_ = frozen(service)
    with service.lock, service.sessions() as db:
        service.control(db, task_id, "message", "Steer: please ignore the labels split.")
        row = service.row(db, task_id)
        service.request_cancel(row, {"code": "user_stop", "message": "intent marker"})
        db.commit()
    bridge = Scripted([("submit_review", {"verdict": "pass", "findings": []})])
    review(service, task_id, bridge)
    paper = json.loads(bridge.prompt)["paper"]
    assert "Steer: please ignore" not in bridge.prompt and "intent marker" not in bridge.prompt
    assert "user_messages" not in paper and "cancel_intent" not in paper
    assert "paper_available" in paper
