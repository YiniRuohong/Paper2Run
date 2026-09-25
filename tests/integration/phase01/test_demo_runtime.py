import hashlib

from fastapi.testclient import TestClient

from p2r_api import main
from p2r_artifacts.store import LocalArtifactStore
from p2r_kernel.supervisor import KernelSupervisor


def test_default_app_does_not_enable_local_execution(tmp_path):
    app = main.create_app(db_url=f"sqlite:///{tmp_path / 'disabled.sqlite3'}")
    assert app.state.runtime['supervisor'] is None
    assert app.state.runtime['artifact_store'] is None


def test_explicit_demo_factory_executes_real_cell_and_persists_output(tmp_path, monkeypatch):
    root = tmp_path / 'demo'
    monkeypatch.setenv('P2R_DEMO_DIR', str(root))
    factory = getattr(main, 'create_demo_app', None)
    assert callable(factory), 'explicit local Demo factory is not implemented'
    app = factory()
    assert isinstance(app.state.runtime['supervisor'], KernelSupervisor)
    assert isinstance(app.state.runtime['artifact_store'], LocalArtifactStore)
    assert app.state.runtime['supervisor'].workspace == root / 'workspace'
    with TestClient(app) as client:
        session = client.post('/api/v1/projects/demo/sessions', json={'metadata': {}})
        assert session.status_code == 201
        session_id = session.json()['data']['id']
        url = f'/api/v1/sessions/{session_id}/cells'
        payload = {'session_id': session_id, 'source': 'print(6 * 7)', 'requested_capabilities': []}
        headers = {'Idempotency-Key': 'real-demo-cell'}
        first = client.post(url, json=payload, headers=headers)
        assert first.status_code == 200, first.text
        data = first.json()['data']
        assert data['status'] == 'succeeded'
        assert data['stdout'] == '42\n'
        assert data['error_code'] is None
        assert data['artifact_ids']
        assert data['environment_id']
        assert client.post(url, json=payload, headers=headers).json()['data'] == data
        actions = client.get(f'/api/v1/sessions/{session_id}/actions').json()['data']
        assert len(actions) == 1
    assert (root / 'demo.sqlite3').is_file()
    outputs = [p for p in (root / 'artifacts').rglob('*') if p.is_file()]
    assert outputs, 'real output must be persisted, not just returned in JSON'
    assert any(p.read_bytes() == b'42\n' for p in outputs)
    assert hashlib.sha256(b'42\n').hexdigest() in {p.name for p in outputs}
