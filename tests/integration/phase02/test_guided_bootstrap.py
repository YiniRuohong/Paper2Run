import hashlib
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from p2r_api.main import create_app, create_demo_app
from p2r_artifacts.store import LocalArtifactStore
from p2r_persistence.models import (
    DemoWriteReceiptRecord, ExtractionRecord, MembershipRecord, PaperRecord,
    PaperVersionRecord, ProjectRecord, SourceRefRecord,
)

SOURCE = b"Paper2Run teaching fixture.\nThis is a controlled smoke check of the offline fixture recipe.\nIt is not a research paper and not a full reproduction.\n"
BODY = {"fixture_id": "offline-smoke-v1", "source_processing_consent": True}


@pytest.fixture
def demo(tmp_path):
    app = create_demo_app(db_url=f"sqlite:///{tmp_path / 'demo.sqlite3'}", artifact_store=LocalArtifactStore(tmp_path / 'store'))
    with TestClient(app) as client:
        yield client, app.state.runtime


def post(client, path, body, key):
    return client.post('/api/v1' + path, json=body, headers={'Idempotency-Key': key})


def bootstrap(client, key='bootstrap'):
    result = post(client, '/demo/bootstrap', BODY, key)
    assert result.status_code == 201, result.text
    return result.json()['data']


def assert_receipt(client, key, path, response):
    receipt = client.get('/api/v1/demo/write-receipts/' + key)
    assert receipt.status_code == 200, receipt.text
    data = receipt.json()['data']
    assert data == {'state': 'completed', 'target_path': '/api/v1' + path,
                    'request_sha256': hashlib.sha256(response.request.content).hexdigest(),
                    'response': response.json()['data']}


def test_explicit_demo_only_and_get_has_no_business_writes(tmp_path, demo):
    for kwargs in ({}, {'development_auth': True}, {'supervisor': object()}):
        app = create_app(db_url=f"sqlite:///{tmp_path / 'normal.sqlite3'}", **kwargs)
        with TestClient(app) as client:
            assert client.get('/api/v1/demo').status_code == 404
            assert post(client, '/demo/bootstrap', BODY, 'key').status_code == 404
    client, runtime = demo
    for _ in range(2):
        data = client.get('/api/v1/demo').json()['data']
        assert data['actor_id'] == 'demo-user'
        assert data['fixture']['id'] == 'offline-smoke-v1'
        assert data['fixture']['source_kind'] == 'text_fixture'
        assert data['labels'] == ['local demo', 'controlled smoke', 'not full reproduction']
    with runtime['session_factory']() as db:
        for model in (ProjectRecord, PaperRecord, ExtractionRecord, DemoWriteReceiptRecord):
            assert db.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.parametrize('body', [
    {}, {'fixture_id': 'offline-smoke-v1'},
    {**BODY, 'source_processing_consent': False},
    {**BODY, 'source_processing_consent': 'true'},
    {**BODY, 'source_processing_consent': 1},
    {**BODY, 'fixture_id': 'unknown'}, {**BODY, 'actor_id': 'other'},
])
def test_strict_bootstrap_validation(demo, body):
    client, _ = demo
    assert post(client, '/demo/bootstrap', body, 'bad').status_code == 422


def test_bootstrap_replay_source_and_extraction(demo):
    client, runtime = demo
    assert client.post('/api/v1/demo/bootstrap', json=BODY).status_code == 422
    first = post(client, '/demo/bootstrap', BODY, 'boot')
    assert first.status_code == 201, first.text
    data = first.json()['data']
    assert post(client, '/demo/bootstrap', BODY, 'boot').json()['data'] == data
    # Whitespace changes are different original request bytes even for the same JSON value.
    assert client.post('/api/v1/demo/bootstrap', content=json.dumps(BODY, indent=2), headers={'Idempotency-Key': 'boot', 'Content-Type': 'application/json'}).status_code == 409
    assert_receipt(client, 'boot', '/demo/bootstrap', first)
    paper = client.get('/api/v1/papers/' + data['paper_id']).json()['data']
    assert paper['current_extraction_id'] == data['extraction_id']
    assert paper['page_count'] == 0
    source = client.get('/api/v1/papers/' + data['paper_id'] + '/content')
    assert source.content == SOURCE
    assert source.headers['content-type'].startswith('text/plain')
    assert hashlib.sha256(source.content).hexdigest() == paper['sha256']
    extraction = client.get('/api/v1/papers/' + data['paper_id'] + '/extraction').json()['data']
    assert extraction['candidate_experiments'] == [{'id': 'offline-smoke', 'recipe_id': 'offline_fixture', 'recipe_version': '1', 'label': 'Controlled smoke', 'execution_support': 'supported', 'provenance': 'manually_configured_recipe'}]
    for ref in extraction['source_refs']:
        assert ref['source_kind'] == 'text_fixture'
        assert ref['source_sha256'] == paper['sha256']
        assert ref['page'] is None
        assert SOURCE[ref['byte_start']:ref['byte_end']].decode('utf-8') == ref['quote']
    assert extraction['source_refs']
    for key in ('extract1', 'extract2'):
        reextract = post(client, '/papers/' + data['paper_id'] + '/extractions', {'reason': 'retry'}, key)
        assert reextract.status_code == 202, reextract.text
        assert reextract.json()['data']['extraction_id'] == data['extraction_id']
    with runtime['session_factory']() as db:
        assert db.scalar(select(func.count()).select_from(ProjectRecord)) == 1
        assert db.scalar(select(func.count()).select_from(ExtractionRecord)) == 1
        assert db.scalar(select(func.count()).select_from(SourceRefRecord)) == 0
        version = db.scalar(select(PaperVersionRecord))
        assert (version.source_kind, version.media_type, version.page_count) == ('text_fixture', 'text/plain', 0)


def test_receipt_permissions_and_global_target_conflict(demo):
    client, runtime = demo
    data = bootstrap(client)
    assert client.get('/api/v1/demo/write-receipts/missing').status_code == 404
    assert client.get('/api/v1/demo/write-receipts/bootstrap', headers={'X-Actor-ID': 'other'}).status_code == 404
    body = {'paper_id': data['paper_id'], 'extraction_id': data['extraction_id'], 'candidate_experiment_id': 'offline-smoke', 'recipe_id': 'offline_fixture', 'recipe_version': '1'}
    assert post(client, '/projects/' + data['project_id'] + '/experiments', body, 'bootstrap').status_code == 409
    assert client.get('/api/v1/projects/' + data['project_id'] + '/experiments').json()['data'] == []
    with runtime['session_factory']() as db:
        db.execute(delete(MembershipRecord).where(MembershipRecord.project_id == data['project_id']))
        db.commit()
    assert client.get('/api/v1/demo/write-receipts/bootstrap').status_code == 404
    assert post(client, '/demo/bootstrap', BODY, 'bootstrap').status_code == 404


def test_content_integrity_and_transaction_rollback(demo, monkeypatch):
    client, runtime = demo
    original = runtime['artifact_store'].put_bytes
    def fail(_):
        raise OSError('controlled store failure')
    monkeypatch.setattr(runtime['artifact_store'], 'put_bytes', fail)
    with TestClient(client.app, raise_server_exceptions=False) as failing:
        assert post(failing, '/demo/bootstrap', BODY, 'failed').status_code >= 500
    with runtime['session_factory']() as db:
        for model in (ProjectRecord, MembershipRecord, PaperRecord, PaperVersionRecord, ExtractionRecord, DemoWriteReceiptRecord):
            assert db.scalar(select(func.count()).select_from(model)) == 0
    monkeypatch.setattr(runtime['artifact_store'], 'put_bytes', original)
    data = bootstrap(client, 'failed')
    monkeypatch.setattr(runtime['artifact_store'], 'open_bytes', lambda _: b'corrupted')
    assert client.get('/api/v1/papers/' + data['paper_id'] + '/content').status_code == 422
    assert post(client, '/papers/' + data['paper_id'] + '/extractions', {'reason': 'retry'}, 'bad-source').status_code == 422


def test_receipt_report_build_allowlist(demo):
    from types import SimpleNamespace
    from p2r_api.demo_receipts import enabled
    client, _ = demo
    request = SimpleNamespace(app=client.app, url=SimpleNamespace(path='/api/v1/runs/run-1/report-builds'))
    assert enabled(request)
    request.url.path = '/api/v1/runs/run-1/report/build'
    assert not enabled(request)


@pytest.mark.parametrize('target', ['/api/v1/runs/run-1/report/build', '/api/v1/private/future-write'])
def test_receipt_reader_rejects_non_allowlisted_records(demo, target):
    client, runtime = demo
    data = bootstrap(client)
    with runtime['session_factory']() as db:
        db.add(DemoWriteReceiptRecord(actor_id='demo-user', key='injected',
                                     target_path=target, request_sha256='0' * 64,
                                     project_id=data['project_id'], response_json={'private': 'must not expose'}))
        db.commit()
    result = client.get('/api/v1/demo/write-receipts/injected')
    assert result.status_code == 404
    assert result.json()['error']['code'] == 'RESOURCE_NOT_FOUND'
    assert 'must not expose' not in result.text


def test_pdf_upload_content_and_extraction_unchanged(demo):
    client, _ = demo
    data = bootstrap(client)
    pdf = b'%PDF-1.4\n/Type /Page\n(Real PDF text)\n%%EOF'
    result = client.post('/api/v1/projects/' + data['project_id'] + '/papers',
                         files={'file': ('paper.pdf', pdf, 'application/pdf')},
                         data={'source_processing_consent': 'true'},
                         headers={'Idempotency-Key': 'pdf'})
    assert result.status_code == 202, result.text
    paper_id = result.json()['data']['paper_id']
    content = client.get('/api/v1/papers/' + paper_id + '/content')
    assert content.content == pdf
    assert content.headers['content-type'] == 'application/pdf'
    result = post(client, '/papers/' + paper_id + '/extractions', {'reason': 'parse'}, 'pdf-extraction')
    assert result.status_code == 202, result.text
    extraction = client.get('/api/v1/papers/' + paper_id + '/extraction').json()['data']
    assert extraction['source_refs'][0]['page'] == 1
    assert extraction['source_refs'][0]['quote'] == 'Real PDF text'
    # Non-whitelisted writes never enter the demo receipt reader.
    assert client.get('/api/v1/demo/write-receipts/pdf').status_code == 404


def test_receipt_failure_rolls_back_complete_business_chain(demo, monkeypatch):
    from p2r_api.routes import demo as demo_routes
    client, runtime = demo
    def fail(*args):
        raise RuntimeError('receipt unavailable')
    monkeypatch.setattr(demo_routes, 'save', fail)
    with TestClient(client.app, raise_server_exceptions=False) as failing:
        assert post(failing, '/demo/bootstrap', BODY, 'rollback').status_code == 500
    with runtime['session_factory']() as db:
        for model in (ProjectRecord, MembershipRecord, PaperRecord, PaperVersionRecord, ExtractionRecord, DemoWriteReceiptRecord):
            assert db.scalar(select(func.count()).select_from(model)) == 0


def test_concurrent_bootstrap_reuses_one_chain(demo):
    from concurrent.futures import ThreadPoolExecutor
    client, runtime = demo
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(post, client, '/demo/bootstrap', BODY, 'concurrent') for _ in range(2)]
        results = [future.result() for future in futures]
    assert [result.status_code for result in results] == [201, 201]
    assert results[0].json()['data'] == results[1].json()['data']
    with runtime['session_factory']() as db:
        assert db.scalar(select(func.count()).select_from(ProjectRecord)) == 1


def test_http_chain_receipts(demo):
    client, _ = demo
    data = bootstrap(client)
    path = '/projects/' + data['project_id'] + '/experiments'
    result = post(client, path, {'paper_id': data['paper_id'], 'extraction_id': data['extraction_id'], 'candidate_experiment_id': 'offline-smoke', 'recipe_id': 'offline_fixture', 'recipe_version': '1'}, 'experiment')
    assert result.status_code == 201, result.text
    assert_receipt(client, 'experiment', path, result)
    exp = result.json()['data']['experiment_id']
    draft = client.get('/api/v1/experiments/' + exp + '/draft').json()['data']
    path = '/projects/' + data['project_id'] + '/spec-revisions'
    result = post(client, path, {'experiment_id': exp, 'base_revision_id': None, 'spec': draft['spec']}, 'revision')
    assert result.status_code == 201, result.text
    assert_receipt(client, 'revision', path, result)
    revision = result.json()['data']
    path = '/spec-revisions/' + revision['id'] + '/confirm'
    result = post(client, path, {'spec_hash': revision['spec_hash'], 'policy_hash': revision['policy_hash'], 'accept_budget': True}, 'confirmation')
    assert result.status_code == 201, result.text
    assert_receipt(client, 'confirmation', path, result)
    confirmation = result.json()['data']['confirmation_id']
    path = '/projects/' + data['project_id'] + '/runs'
    result = post(client, path, {'spec_revision_id': revision['id'], 'confirmation_id': confirmation}, 'run')
    assert result.status_code == 202, result.text
    assert_receipt(client, 'run', path, result)
    run = result.json()['data']['run_id']
    for suffix, body in [('cancel', {'reason': 'test'}), ('exports', {'formats': ['report_json']})]:
        path = '/runs/' + run + '/' + suffix
        result = post(client, path, body, suffix)
        assert result.status_code == 202, result.text
        assert_receipt(client, suffix, path, result)
        assert post(client, path, body, suffix).json()['data'] == result.json()['data']
