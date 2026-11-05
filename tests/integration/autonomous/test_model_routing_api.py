"""HTTP model-routing configuration and auth boundaries, using an isolated database."""
import json

from .test_api import client, write, setup, config, BASE


def test_reuse_profile_credentials_and_routing_snapshot_over_http(client, monkeypatch):
    setup(client)
    payload = {"id": "expert", "base_url": "http://127.0.0.1:1", "model": "expert-model",
               "api": "openai-completions", "reuse_credentials_from": "test"}
    response = write(client, "POST", "/profiles", payload, key="reuse-key")
    assert response.status_code == 201, response.text
    assert response.json()["data"]["secret_configured"] is True
    assert "integration-private-key" not in response.text
    assert "reuse_credentials_from" not in response.json()["data"]
    assert write(client, "POST", "/profiles", payload, key="reuse-key").status_code == 201
    cfg = config()
    cfg["specialists"] = [{"id": "code-expert", "description": "Code review", "task_types": ["coding", "review"],
        "model": {"provider_profile": "expert", "credential_ref": "profile:expert"}}]
    saved = write(client, "PUT", "/projects/project/config", {"config": cfg}, headers={"If-Match": "1"})
    assert saved.status_code == 200, saved.text
    assert client.get(BASE + "/projects/project/config").json()["data"]["config"]["specialists"] == cfg["specialists"]
    service = client.app.state.runtime["autonomous_service"]
    launched = []
    monkeypatch.setattr(service, "launch", lambda task_id, *args: launched.append(task_id))
    task = write(client, "POST", "/projects/project/tasks", {"objective": "Test routing", "authorization": {"confirmed": True}})
    assert task.status_code == 202, task.text
    body = task.json()["data"]
    assert launched == [body["id"]]
    assert body["config_snapshot"]["specialist_profiles"]["code-expert"]["model"] == "expert-model"
    assert "integration-private-key" not in json.dumps(body)
    with service.sessions() as db:
        main, specialists = service.resolve_models(service.row(db, body["id"]))
    assert specialists[0]["model"]["api_key"] == main["api_key"]


def test_http_credential_reuse_cannot_change_destination(client):
    setup(client)
    payload = {"id": "expert", "base_url": "https://elsewhere.invalid", "model": "expert-model",
               "api": "openai-completions", "reuse_credentials_from": "test"}
    response = write(client, "POST", "/profiles", payload)
    assert response.status_code == 403, response.text
    assert "integration-private-key" not in response.text
    assert {p["id"] for p in client.get(BASE + "/profiles").json()["data"]} == {"test"}
