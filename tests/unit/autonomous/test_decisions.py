"""Explicit approve/reject of a pending protocol decision (contract C2)."""
import pytest
from sqlalchemy import select

from p2r_orchestration.autonomous.worker import dispatch
from p2r_orchestration.errors import ServiceError
from p2r_persistence.autonomous import AutonomousEventRecord as Event

from .test_driver import Main
from .test_service import Target, create, protocol, service, wait  # noqa: F401

REJECTED = "Protocol proposal {} was rejected by the user; the current protocol stays in force."


def pending(service):
    """A paused task holding a protocol proposal, as a running Pi would leave it."""
    task_id = create(service, protocol=protocol())
    with service.sessions() as db:
        row = service.row(db, task_id); row.state = "running"; db.commit()
    proposal = {**protocol(), "method": "changed method"}
    result = dispatch(service, Target({}, service.root), task_id, "propose_protocol",
                      {"protocol": proposal, "reason": "change"}, "p1")
    assert result["paused"]
    return task_id, result["pending_decision"]


def events(service, task_id, kind):
    with service.sessions() as db:
        return [e.payload for e in db.scalars(select(Event).where(Event.task_id == task_id, Event.type == kind)
                                                 .order_by(Event.sequence)).all()]


def refused(call, code):
    with pytest.raises(ServiceError) as caught:
        call()
    assert (caught.value.code, caught.value.status) == (code, 409)


def test_approve_applies_protocol_and_queues(service):
    task_id, decision = pending(service)
    with service.sessions() as db:
        row = service.decide(db, task_id, decision["id"], "approve")
        assert row.state == "queued" and row.pending_decision_json is None
        assert row.protocol_json == decision["proposed"]
        assert row.reference_sources_confirmed is False and row.report_json is None
        db.commit()
    approved = events(service, task_id, "protocol_approved")
    assert [p["decision_id"] for p in approved] == [decision["id"]]
    assert events(service, task_id, "task_resumed")
    assert not events(service, task_id, "protocol_rejected")


def test_reject_keeps_protocol_and_reaches_next_prompt_once(service):
    task_id, decision = pending(service)
    text = REJECTED.format(decision["id"]) + " Note: keep the regression method"
    with service.sessions() as db:
        before = service.row(db, task_id).protocol_json
        row = service.decide(db, task_id, decision["id"], "reject", "keep the regression method")
        assert row.state == "queued" and row.pending_decision_json is None
        assert row.protocol_json == before and row.protocol_json["protocol"]["method"] == "regression"
        assert row.context_json["user_messages"][-1] == text
        db.commit()
    assert events(service, task_id, "protocol_rejected") == [{
        "decision_id": decision["id"], "proposed_sha256": decision["proposed"]["sha256"],
        "changed": decision["changed"], "note": "keep the regression method"}]
    assert events(service, task_id, "user_message") == [{"message": text}]
    assert not events(service, task_id, "protocol_approved")
    main = Main()
    service.bridge_factory = lambda: main
    service.launch(task_id)
    wait(service, task_id)
    assert text in main.prompts[0]
    assert all(text not in p for p in main.prompts[1:])


def test_reject_without_note_has_no_note_line(service):
    task_id, decision = pending(service)
    with service.sessions() as db:
        row = service.decide(db, task_id, decision["id"], "reject")
        assert row.context_json["user_messages"][-1] == REJECTED.format(decision["id"])
        db.commit()
    assert events(service, task_id, "protocol_rejected")[0]["note"] is None


def test_mismatched_or_absent_decision_is_refused_without_change(service):
    task_id, decision = pending(service)
    with service.sessions() as db:
        refused(lambda: service.decide(db, task_id, "other-id", "approve"), "DECISION_MISMATCH")
        row = service.row(db, task_id)
        assert row.pending_decision_json == decision and row.state == "paused"
    assert not events(service, task_id, "protocol_approved")
    plain = create(service)
    with service.sessions() as db:
        row = service.row(db, plain); row.state = "paused"; db.commit()
        refused(lambda: service.decide(db, plain, decision["id"], "reject"), "DECISION_MISMATCH")


@pytest.mark.parametrize("state", ["running", "queued", "stopping"])
def test_decision_requires_paused_task(service, state):
    task_id, decision = pending(service)
    with service.sessions() as db:
        row = service.row(db, task_id); row.state = state; db.commit()
        refused(lambda: service.decide(db, task_id, decision["id"], "approve"), "INVALID_STATE")


def test_legacy_and_finished_tasks_are_refused(service):
    task_id, decision = pending(service)
    with service.sessions() as db:
        row = service.row(db, task_id); row.harness_json = None; db.commit()
        refused(lambda: service.decide(db, task_id, decision["id"], "approve"), "LEGACY_TASK")
    task_id, decision = pending(service)
    with service.sessions() as db:
        row = service.row(db, task_id); row.outcome = "verified"; row.state = "completed"; db.commit()
        refused(lambda: service.decide(db, task_id, decision["id"], "reject"), "TASK_FINISHED")


def test_decision_reuses_resume_preconditions(service):
    task_id, decision = pending(service)
    with service.sessions() as db:
        service.request_cancel(service.row(db, task_id)); db.commit()
        refused(lambda: service.decide(db, task_id, decision["id"], "approve"), "CANCELLATION_PENDING")


def test_unknown_action_is_rejected(service):
    task_id, decision = pending(service)
    with service.sessions() as db:
        with pytest.raises(ServiceError) as caught:
            service.decide(db, task_id, decision["id"], "defer")
        assert caught.value.code == "VALIDATION_ERROR"


def test_plain_resume_while_pending_reports_decision_pending(service):
    task_id, decision = pending(service)
    with service.sessions() as db:
        refused(lambda: service.control(db, task_id, "resume"), "DECISION_PENDING")
        refused(lambda: service.control(db, task_id, "resume", "approve:other"), "PROTOCOL_APPROVAL_REQUIRED")
        assert service.row(db, task_id).pending_decision_json == decision


def test_legacy_approve_message_still_works(service):
    task_id, decision = pending(service)
    with service.sessions() as db:
        row = service.control(db, task_id, "resume", "approve:" + decision["id"])
        assert row.state == "queued" and row.pending_decision_json is None
        assert row.protocol_json == decision["proposed"]
        db.commit()
    assert [p["decision_id"] for p in events(service, task_id, "protocol_approved")] == [decision["id"]]
