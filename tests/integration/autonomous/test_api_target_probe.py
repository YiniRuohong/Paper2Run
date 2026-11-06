"""HTTP wiring of the C6 target probe with a fake execution target (no Docker)."""
from __future__ import annotations

from .test_api import BASE, client, config, write  # noqa: F401  (client is a fixture)


class FakeTarget:
    built = []

    def __init__(self, cfg, state_dir):
        FakeTarget.built.append((cfg, state_dir))

    def probe(self, timeout):
        return {"image_available": True, "docker_os_type": "linux", "docker_server_version": "27.3.1"}

    def observe_gpus(self, timeout):
        return []


def test_probe_requires_saved_config_then_reports_observations(client):
    service = client.app.state.runtime["autonomous_service"]
    service.target_factory = FakeTarget
    FakeTarget.built.clear()
    missing = client.get(BASE + "/projects/project/target/probe")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert write(client, "PUT", "/projects/project/config", {"config": config()}).status_code == 200
    response = client.get(BASE + "/projects/project/target/probe")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["target_kind"] == "local" and data["isolation"]["verified"] is True
    assert data["docker"] == {"available": True, "os_type": "linux", "server_version": "27.3.1"}
    assert data["network_policy"]["verified"] is False and data["network_policy"]["public_internet"] is False
    assert data["missing"] == [] and response.json()["meta"]["request_id"]
    # Probes the project workspace root; the state dir sits under control storage.
    cfg, state_dir = FakeTarget.built[-1]
    assert cfg["workspace"].endswith("workspace") and "target-probes" in str(state_dir)


def test_probe_needs_membership_and_strict_query(client):
    service = client.app.state.runtime["autonomous_service"]
    service.target_factory = FakeTarget
    assert write(client, "PUT", "/projects/project/config", {"config": config()}).status_code == 200
    assert client.get(BASE + "/projects/project/target/probe?x=1").status_code == 422
    client.cookies.set("p2r_session", client.secrets["other"])
    assert client.get(BASE + "/projects/project/target/probe").status_code in (403, 404)
