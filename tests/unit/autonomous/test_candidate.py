import hashlib
import json

import pytest
from sqlalchemy import select

from p2r_orchestration.autonomous import candidate, evidence
from p2r_orchestration.autonomous.tools import SYSTEM_PROMPT, TOOLS
from p2r_orchestration.autonomous.worker import dispatch
from p2r_orchestration.service import ServiceError
from p2r_persistence.autonomous import AutonomousEventRecord as Event

from .test_service import Target, create, protocol, service  # noqa: F401

KINDS = [("main.py", "code"), ("env.json", "environment"), ("data.json", "data"), ("predictions.json", "result"),
         ("labels.json", "result")]


def running(service):
    task_id = create(service, protocol=protocol())
    with service.sessions() as db:
        row = service.row(db, task_id)
        row.state = "running"
        db.commit()
    return task_id, Target({}, service.root)


def register(service, target, task_id, name, kind, content=None):
    target.write(name, content or f"content of {name}")
    return dispatch(service, target, task_id, "register_artifact", {"path": name, "name": name, "kind": kind}, f"r-{name}")


def run(service, target, task_id, launch_id):
    return dispatch(service, target, task_id, "execute",
                    {"command": "python3 main.py", "timeout_seconds": 5}, launch_id)


def record_tool_result(service, task_id, name, result):
    with service.sessions() as db:
        row = service.row(db, task_id)
        service.event(db, row, "tool_completed", {"name": name, "result": result})
        db.commit()


def complete(service, target, task_id):
    for name, kind in KINDS[:1]:
        register(service, target, task_id, name, kind)
    dispatch_run = run(service, target, task_id, "formal")
    record_tool_result(service, task_id, "execute", dispatch_run)
    for name, kind in KINDS[1:]:
        register(service, target, task_id, name, kind)


def gate(service, task_id):
    with service.sessions() as db:
        return candidate.gate(db, service.row(db, task_id))


def submit(service, target, task_id, summary="done"):
    return dispatch(service, target, task_id, "submit_candidate", {"summary": summary}, "submit")


def test_gate_lists_missing_items(service):
    task_id = create(service)
    assert gate(service, task_id) == [
        "frozen protocol", "code artifact", "environment artifact", "data artifact", "result artifact",
        "entrypoint code artifact", "successful entrypoint run after latest code registration"]


def test_gate_requires_entrypoint_code_and_expected_artifacts(service):
    task_id, target = running(service)
    register(service, target, task_id, "helper.py", "code")
    record_tool_result(service, task_id, "execute", run(service, target, task_id, "formal"))
    for name, kind in KINDS[1:4]:
        register(service, target, task_id, name, kind)
    assert gate(service, task_id) == ["entrypoint code artifact", "expected artifact labels.json"]
    register(service, target, task_id, "labels.json", "result")
    register(service, target, task_id, "main.py", "code")
    record_tool_result(service, task_id, "execute", run(service, target, task_id, "after-entrypoint"))
    assert gate(service, task_id) == []


def test_gate_requires_run_after_latest_code(service):
    task_id, target = running(service)
    for name, kind in KINDS[1:]:
        register(service, target, task_id, name, kind)
    record_tool_result(service, task_id, "execute", run(service, target, task_id, "early"))
    register(service, target, task_id, "main.py", "code")
    assert gate(service, task_id) == ["successful entrypoint run after latest code registration"]
    record_tool_result(service, task_id, "execute", run(service, target, task_id, "late"))
    assert gate(service, task_id) == []


def test_submit_candidate_incomplete_is_409(service):
    task_id, target = running(service)
    with pytest.raises(ServiceError) as caught:
        submit(service, target, task_id)
    assert caught.value.code == "CANDIDATE_INCOMPLETE" and caught.value.status == 409
    assert "code artifact" in str(caught.value)


def test_submit_candidate_freezes_copies(service):
    task_id, target = running(service)
    complete(service, target, task_id)
    result = submit(service, target, task_id, "my summary")
    assert result["status"] == "candidate_submitted" and result["round"] == 0
    with service.sessions() as db:
        row = service.row(db, task_id)
        snap = row.harness_json["candidate"]
        assert snap["snapshot_sha256"] == result["snapshot_sha256"]
        assert snap["review_status"] == "pending" and snap["attempts"] == 0 and snap["summary"] == "my summary"
        assert {a["kind"] for a in snap["artifacts"]} == {"code", "environment", "data", "result"}
        base = service.task_root(task_id) / "candidates" / snap["snapshot_sha256"]
        for item in snap["artifacts"]:
            raw = (base / "artifacts" / item["name"]).read_bytes()
            assert hashlib.sha256(raw).hexdigest() == item["sha256"]
        log = base / "logs" / f"{snap['run']['job_id']}.txt"
        assert log.is_file() and log.read_text().startswith("observed")
        assert snap["run"]["exit_code"] == 0 and snap["run"]["sequence"] > 0
        events = db.scalars(select(Event).where(Event.task_id == task_id, Event.type == "candidate_frozen")).all()
        assert len(events) == 1 and events[0].payload["snapshot_sha256"] == snap["snapshot_sha256"]


def test_logs_are_paged_from_stdout(service):
    task_id, target = running(service)
    pages = {0: ("aa", 2), 2: ("bb", 4), 4: ("cc", 4)}

    def logs(job_id, offset=0, limit=16000):
        text, following = pages[offset]
        return {"stdout": text, "offset": offset, "next_offset": following}
    target.logs = logs
    complete(service, target, task_id)
    submit(service, target, task_id)
    with service.sessions() as db:
        snap = service.row(db, task_id).harness_json["candidate"]
        path = service.task_root(task_id) / "candidates" / snap["snapshot_sha256"] / "logs" / f"{snap['run']['job_id']}.txt"
        assert path.read_text() == "aabbcc"


def test_register_refused_while_candidate_under_review(service):
    task_id, target = running(service)
    complete(service, target, task_id)
    submit(service, target, task_id)
    with service.sessions() as db:
        before = service.row(db, task_id).harness_json["candidate"]
    with pytest.raises(ServiceError) as caught:
        register(service, target, task_id, "extra.json", "other")
    assert caught.value.code == "CANDIDATE_UNDER_REVIEW" and caught.value.status == 409
    with pytest.raises(ServiceError) as again:
        submit(service, target, task_id)
    assert again.value.code == "CANDIDATE_UNDER_REVIEW"
    with service.sessions() as db:
        assert service.row(db, task_id).harness_json["candidate"] == before


def test_propose_protocol_refused_while_candidate_under_review(service):
    task_id, target = running(service)
    complete(service, target, task_id)
    submit(service, target, task_id)
    with service.sessions() as db:
        before = service.row(db, task_id).protocol_json
    proposal = {**protocol(), "method": "changed while frozen"}
    with pytest.raises(ServiceError) as caught:
        dispatch(service, target, task_id, "propose_protocol", {"protocol": proposal, "reason": "x"}, "p")
    assert caught.value.code == "CANDIDATE_UNDER_REVIEW"
    with service.sessions() as db:
        row = service.row(db, task_id)
        assert row.protocol_json == before and row.pending_decision_json is None and row.state == "running"


def test_evaluate_and_report_not_exposed(service):
    names = {t["name"] for t in TOOLS}
    assert not names & {"evaluate", "report"} and {"note_progress", "submit_candidate"} <= names
    task_id, target = running(service)
    for name, args in (("evaluate", {}), ("report", {"narrative": "x"})):
        with pytest.raises(ValueError):
            dispatch(service, target, task_id, name, args, name)


def test_note_progress_records_event(service):
    task_id, target = running(service)
    assert dispatch(service, target, task_id, "note_progress", {"phase": "setup", "summary": "installing"}, "n") == {"recorded": True}
    with service.sessions() as db:
        event = db.scalars(select(Event).where(Event.task_id == task_id, Event.type == "progress_noted")).one()
        assert event.payload == {"phase": "setup", "summary": "installing"}


def test_prompt_has_no_step_order():
    assert "submit_candidate" in SYSTEM_PROMPT and "max_repair_rounds" in SYSTEM_PROMPT
    assert "protocol entrypoint" in SYSTEM_PROMPT and "expected_artifacts" in SYSTEM_PROMPT
    for phrase in ("Step 1", "first,", "then "):
        assert phrase not in SYSTEM_PROMPT


def old_run_then_new_code(service):
    task_id, target = running(service)
    register(service, target, task_id, "main.py", "code")
    first = run(service, target, task_id, "formal")
    record_tool_result(service, task_id, "execute", first)
    for name, kind in KINDS[1:]:
        register(service, target, task_id, name, kind)
    assert gate(service, task_id) == []
    register(service, target, task_id, "main2.py", "code")
    return task_id, target, first


MISSING_RUN = ["successful entrypoint run after latest code registration"]


def test_status_poll_of_old_job_does_not_requalify(service):
    task_id, target, first = old_run_then_new_code(service)
    polled = dispatch(service, target, task_id, "status", {"job_id": first["job_id"]}, "poll")
    record_tool_result(service, task_id, "status", polled)
    assert gate(service, task_id) == MISSING_RUN


def test_reused_launch_id_does_not_requalify(service):
    task_id, target, first = old_run_then_new_code(service)
    again = run(service, target, task_id, "formal")
    record_tool_result(service, task_id, "execute", again)
    assert again["job_id"] == first["job_id"]
    assert gate(service, task_id) == MISSING_RUN


def test_background_job_started_before_code_does_not_requalify(service):
    task_id, target = running(service)
    register(service, target, task_id, "main.py", "code")
    started = dispatch(service, target, task_id, "background",
                       {"command": "python3 main.py", "launch_id": "bg", "timeout_seconds": 5}, "bg")
    for name, kind in KINDS[1:]:
        register(service, target, task_id, name, kind)
    register(service, target, task_id, "main2.py", "code")
    record_tool_result(service, task_id, "status", dispatch(service, target, task_id, "status", {"job_id": started["job_id"]}, "p"))
    assert gate(service, task_id) == MISSING_RUN


def test_snapshot_records_launch_sequence(service):
    task_id, target = running(service)
    complete(service, target, task_id)
    submit(service, target, task_id)
    with service.sessions() as db:
        row = service.row(db, task_id)
        job = next(iter(row.jobs_json.values()))
        assert row.harness_json["candidate"]["run"]["sequence"] == job["sequence"]


def test_job_without_launch_sequence_does_not_qualify(service):
    task_id, target = running(service)
    complete(service, target, task_id)
    with service.sessions() as db:
        row = service.row(db, task_id)
        row.jobs_json = {k: {a: b for a, b in v.items() if a != "sequence"} for k, v in row.jobs_json.items()}
        db.commit()
    assert gate(service, task_id) == MISSING_RUN


@pytest.mark.parametrize("change", ["round", "protocol"])
def test_freeze_refuses_stale_candidate(service, change):
    task_id, target = running(service)
    complete(service, target, task_id)

    def logs(job_id, offset=0, limit=16000):
        with service.sessions() as db:
            row = service.row(db, task_id)
            if change == "round":
                row.harness_json = {**row.harness_json, "repair_round": 1}
            else:
                row.protocol_json = {**row.protocol_json, "sha256": "f" * 64}
            db.commit()
        return {"stdout": "x", "next_offset": 0}
    target.logs = logs
    with pytest.raises(ServiceError) as caught:
        submit(service, target, task_id)
    assert caught.value.code == "CANDIDATE_STALE"
    with service.sessions() as db:
        assert service.row(db, task_id).harness_json["candidate"] is None


def test_freeze_detects_tampered_artifact(service):
    task_id, target = running(service)
    complete(service, target, task_id)
    with service.sessions() as db:
        root = evidence.artifact_root(service, service.row(db, task_id))
    (root / "main.py").write_text("tampered")
    with pytest.raises(ServiceError) as caught:
        submit(service, target, task_id)
    assert caught.value.code == "EVIDENCE_INTEGRITY"
    with service.sessions() as db:
        assert service.row(db, task_id).harness_json["candidate"] is None


def test_freeze_refuses_unattributable_job_id(service):
    task_id, target = running(service)
    register(service, target, task_id, "main.py", "code")
    for name, kind in KINDS[1:]:
        register(service, target, task_id, name, kind)
    with service.sessions() as db:
        row = service.row(db, task_id)
        sha = row.protocol_json["sha256"]
        row.jobs_json = {"l": {"job_id": "../evil", "command": "python3 main.py", "protocol_sha256": sha,
                               "sequence": row.event_sequence + 1}}
        service.event(db, row, "tool_completed", {"name": "execute", "result": {"job_id": "../evil", "exit_code": 0}})
        db.commit()
    with pytest.raises(ServiceError) as caught:
        submit(service, target, task_id)
    assert caught.value.code == "CANDIDATE_INCOMPLETE"


def test_prompt_names_program_outcomes():
    for outcome in ("verified", "completed_with_issues", "review_unavailable", "blocked_by_safety"):
        assert outcome in SYSTEM_PROMPT
    assert "needs_human" not in SYSTEM_PROMPT and "Model text cannot establish success" in SYSTEM_PROMPT


def test_initial_prompt_states_max_repair_rounds(service):
    from .test_service import Bridge
    seen = []

    class Capturing(Bridge):
        def run(self, config, prompt, callback):
            seen.append(prompt)
    service.bridge_factory = Capturing
    task_id = create(service)
    service.launch(task_id)
    service.workers[task_id]["thread"].join(timeout=10)
    assert json.loads(seen[0])["max_repair_rounds"] == 3
