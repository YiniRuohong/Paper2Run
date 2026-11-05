"""GET /api/v1/artifacts/{id}/raw: verified bytes with inert preview headers."""
from fastapi.testclient import TestClient

from p2r_artifacts.store import LocalArtifactStore
from p2r_persistence.models import AttemptRecord

from test_attempts_http import H, app, headers  # noqa: F401  (fixture reuse)


def _register(app, tmp_path, content, path, media_type):
    store = LocalArtifactStore(tmp_path / "artifacts")
    app.state.runtime["artifact_store"] = store
    address = store.put_bytes(content)
    with app.state.runtime["session_factory"].begin() as db:
        db.add(AttemptRecord(id="raw-att-" + path.replace(".", "-"), run_id="run", attempt_index=5, source="model", status="candidate",
                             base_code_hash=H, validation_json={}, code_manifest_json={"files": [
                                 {"path": path, "artifact_id": address.sha256, "sha256": address.sha256,
                                  "bytes": address.bytes, "media_type": media_type}]}))
    return address.sha256


def test_artifact_raw_happy_path_headers(app, tmp_path):
    content = "結果\nok\n".encode()
    artifact_id = _register(app, tmp_path, content, "out.txt", "text/plain")
    with TestClient(app, base_url="https://testserver") as client:
        response = client.get(f"/api/v1/artifacts/{artifact_id}/raw", headers=headers())
    assert response.status_code == 200, response.text
    assert response.content == content
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"].startswith("sandbox;")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-disposition"] == "inline; filename*=UTF-8''out.txt"


def test_artifact_raw_svg_downgraded(app, tmp_path):
    artifact_id = _register(app, tmp_path, b"<svg><script>alert(1)</script></svg>", "plot.svg", "image/svg+xml")
    with TestClient(app, base_url="https://testserver") as client:
        response = client.get(f"/api/v1/artifacts/{artifact_id}/raw", headers=headers())
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/plain; charset=utf-8"


def test_artifact_raw_unknown_and_access(app, tmp_path):
    artifact_id = _register(app, tmp_path, b"data", "out.bin", "application/octet-stream")
    with TestClient(app, base_url="https://testserver") as client:
        missing = client.get("/api/v1/artifacts/" + "b" * 64 + "/raw", headers=headers())
        assert missing.status_code == 404
        unknown = client.get(f"/api/v1/artifacts/{artifact_id}/raw?offset=1", headers=headers())
        assert unknown.status_code == 422 and unknown.json()["error"]["code"] == "VALIDATION_ERROR"
        outsider = client.get(f"/api/v1/artifacts/{artifact_id}/raw", headers=headers("outsider"))
        assert outsider.status_code == 404
