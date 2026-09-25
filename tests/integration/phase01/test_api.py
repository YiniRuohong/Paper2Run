import hashlib
import json

from fastapi.testclient import TestClient
import pytest

from p2r_api.main import create_app
from p2r_contracts.sessions import CellCreate
from p2r_kernel.protocol import ExecuteCellResult
from p2r_persistence.models import Environment, IdempotencyKey


class FakeSupervisor:
    def execute(self, request):
        return ExecuteCellResult(namespace_hash="a" * 64, stdout="ok\n", result_summary="ok")


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(db_url=f"sqlite:///{tmp_path / 'test.sqlite3'}", supervisor=FakeSupervisor()))


def create_session(client, actor="owner", project="demo", **metadata):
    return client.post(
        f"/api/v1/projects/{project}/sessions",
        headers={"X-Actor-ID": actor},
        json={"metadata": metadata},
    )


def cell_payload(session_id, source="1 + 1"):
    return {"session_id": session_id, "source": source, "requested_capabilities": []}


def checkpoint_payload(environment_id, action_cursor=0):
    return {
        "name": "baseline",
        "action_cursor": action_cursor,
        "namespace_snapshot_hash": "a" * 64,
        "input_artifact_ids": [],
        "environment_id": environment_id,
        "replay_plan": {},
        "manifest_hash": "b" * 64,
    }


def test_create_session_returns_envelope(client):
    response = create_session(client)
    assert response.status_code == 201
    body = response.json()
    assert body["data"]["metadata"]["project_id"] == "demo"
    assert body["meta"]["request_id"]


def test_malformed_cell_json_returns_validation_envelope(client):
    session_id = create_session(client).json()["data"]["id"]
    response = client.post(
        f"/api/v1/sessions/{session_id}/cells",
        headers={"X-Actor-ID": "owner", "Idempotency-Key": "bad-json"},
        content=b"{not-json",
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert response.json()["error"]["details"]["field_errors"]


def test_list_project_sessions_is_scoped_to_actor_and_project(client):
    create_session(client, project="demo")
    create_session(client, project="other")
    create_session(client, actor="other")
    response = client.get("/api/v1/projects/demo/sessions", headers={"X-Actor-ID": "owner"})
    assert response.status_code == 200
    assert len(response.json()["data"]) == 1
    assert response.json()["data"][0]["user_id"] == "owner"
    assert response.json()["data"][0]["metadata"]["project_id"] == "demo"


def test_owner_viewer_isolation_and_unknown_session_are_not_disclosed(client):
    session_id = create_session(client).json()["data"]["id"]
    for actor in ("viewer", "other"):
        response = client.get(f"/api/v1/sessions/{session_id}", headers={"X-Actor-ID": actor})
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"
    response = client.get("/api/v1/sessions/does-not-exist", headers={"X-Actor-ID": "owner"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_cell_idempotency_replay_and_conflict(client):
    session_id = create_session(client).json()["data"]["id"]
    headers = {"X-Actor-ID": "owner", "Idempotency-Key": "cell-1"}
    first = client.post(f"/api/v1/sessions/{session_id}/cells", headers=headers, json=cell_payload(session_id))
    replay = client.post(f"/api/v1/sessions/{session_id}/cells", headers=headers, json=cell_payload(session_id))
    conflict = client.post(f"/api/v1/sessions/{session_id}/cells", headers=headers, json=cell_payload(session_id, "2 + 2"))
    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json()["data"] == first.json()["data"]
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_cell_idempotency_in_progress_returns_conflict(client):
    session_id = create_session(client).json()["data"]["id"]
    db = client.app.state.runtime["session_factory"]()
    try:
        body_hash = hashlib.sha256(CellCreate.model_validate(cell_payload(session_id)).model_dump_json().encode()).hexdigest()
        db.add(IdempotencyKey(scope=f"cell:{session_id}:owner", key="cell-busy", body_hash=body_hash))
        db.commit()
    finally:
        db.close()
    response = client.post(
        f"/api/v1/sessions/{session_id}/cells",
        headers={"X-Actor-ID": "owner", "Idempotency-Key": "cell-busy"},
        json=cell_payload(session_id),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "IDEMPOTENCY_IN_PROGRESS"


def test_checkpoint_idempotency_replay_conflict_and_in_progress(client):
    session_id = create_session(client).json()["data"]["id"]
    cell = client.post(
        f"/api/v1/sessions/{session_id}/cells",
        headers={"X-Actor-ID": "owner", "Idempotency-Key": "cell-for-checkpoint"},
        json=cell_payload(session_id),
    )
    assert cell.status_code == 200
    db = client.app.state.runtime["session_factory"]()
    try:
        environment_id = db.query(Environment).filter_by(session_id=session_id).one().id
    finally:
        db.close()
    payload = checkpoint_payload(environment_id)
    headers = {"X-Actor-ID": "owner", "Idempotency-Key": "checkpoint-1"}
    first = client.post(f"/api/v1/sessions/{session_id}/checkpoints", headers=headers, json=payload)
    replay = client.post(f"/api/v1/sessions/{session_id}/checkpoints", headers=headers, json=payload)
    conflict_payload = {**payload, "name": "different"}
    conflict = client.post(f"/api/v1/sessions/{session_id}/checkpoints", headers=headers, json=conflict_payload)
    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json()["data"] == first.json()["data"]
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    db = client.app.state.runtime["session_factory"]()
    try:
        body_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        db.add(IdempotencyKey(scope=f"checkpoint:{session_id}:owner", key="checkpoint-busy", body_hash=body_hash))
        db.commit()
    finally:
        db.close()
    busy = client.post(
        f"/api/v1/sessions/{session_id}/checkpoints",
        headers={"X-Actor-ID": "owner", "Idempotency-Key": "checkpoint-busy"},
        json=payload,
    )
    assert busy.status_code == 409
    assert busy.json()["error"]["code"] == "IDEMPOTENCY_IN_PROGRESS"


def test_restore_idempotency_replay_conflict_and_in_progress(client):
    session_id = create_session(client).json()["data"]["id"]
    cell = client.post(
        f"/api/v1/sessions/{session_id}/cells",
        headers={"X-Actor-ID": "owner", "Idempotency-Key": "restore-cell"},
        json=cell_payload(session_id),
    )
    assert cell.status_code == 200
    db = client.app.state.runtime["session_factory"]()
    try:
        environment_id = db.query(Environment).filter_by(session_id=session_id).one().id
    finally:
        db.close()
    checkpoint = client.post(
        f"/api/v1/sessions/{session_id}/checkpoints",
        headers={"X-Actor-ID": "owner", "Idempotency-Key": "restore-checkpoint"},
        json=checkpoint_payload(environment_id),
    )
    assert checkpoint.status_code == 200
    checkpoint_id = checkpoint.json()["data"]["id"]
    payload = {"checkpoint_id": checkpoint_id}
    headers = {"X-Actor-ID": "owner", "Idempotency-Key": "restore-1"}
    first = client.post(f"/api/v1/sessions/{session_id}/restore", headers=headers, json=payload)
    replay = client.post(f"/api/v1/sessions/{session_id}/restore", headers=headers, json=payload)
    conflict = client.post(f"/api/v1/sessions/{session_id}/restore", headers=headers, json={"checkpoint_id": "other"})
    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json()["data"] == first.json()["data"]
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    db = client.app.state.runtime["session_factory"]()
    try:
        body_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        db.add(IdempotencyKey(scope=f"restore:{session_id}:owner", key="restore-busy", body_hash=body_hash))
        db.commit()
    finally:
        db.close()
    busy = client.post(
        f"/api/v1/sessions/{session_id}/restore",
        headers={"X-Actor-ID": "owner", "Idempotency-Key": "restore-busy"},
        json=payload,
    )
    assert busy.status_code == 409
    assert busy.json()["error"]["code"] == "IDEMPOTENCY_IN_PROGRESS"


def test_sse_wire_format_cursor_and_last_event_id(client):
    session_id = create_session(client).json()["data"]["id"]
    submitted = client.post(
        f"/api/v1/sessions/{session_id}/cells",
        headers={"X-Actor-ID": "owner", "Idempotency-Key": "cell-events"},
        json=cell_payload(session_id),
    )
    assert submitted.status_code == 200
    response = client.get(f"/api/v1/sessions/{session_id}/events", headers={"X-Actor-ID": "owner"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text.startswith("id: 0\nevent: cell.completed\ndata: {")
    assert response.text.endswith("\n\n")
    assert "\n\n" in response.text
    assert client.get(f"/api/v1/sessions/{session_id}/events?after=0", headers={"X-Actor-ID": "owner"}).text == ""
    resumed = client.get(
        f"/api/v1/sessions/{session_id}/events",
        headers={"X-Actor-ID": "owner", "Last-Event-ID": "-1"},
    )
    assert resumed.text == response.text


def test_openapi_exposes_only_public_routes(client):
    paths = set(client.get("/openapi.json").json()["paths"])
    expected = {
        "/api/v1/projects/{project_id}/sessions",
        "/api/v1/sessions",
        "/api/v1/sessions/{session_id}",
        "/api/v1/sessions/{session_id}/cells",
        "/api/v1/sessions/{session_id}/actions",
        "/api/v1/sessions/{session_id}/checkpoints",
        "/api/v1/sessions/{session_id}/events",
    }
    assert expected <= paths
    assert all("kernel" not in path and "internal" not in path for path in paths)


def test_validation_envelope_for_invalid_checkpoint(client):
    session_id = create_session(client).json()["data"]["id"]
    response = client.post(
        f"/api/v1/sessions/{session_id}/checkpoints",
        headers={"X-Actor-ID": "owner", "Idempotency-Key": "invalid-checkpoint"},
        json={"name": "bad", "action_cursor": 0},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert response.json()["meta"]["request_id"]
