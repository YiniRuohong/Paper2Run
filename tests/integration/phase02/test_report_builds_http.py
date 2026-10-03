import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from p2r_api.main import create_app
from p2r_artifacts.store import LocalArtifactStore
from p2r_evaluation import EVALUATOR_VERSION
from p2r_persistence.job_repository import JobRepository
from p2r_persistence.models import OperationRecord, ExportRecord, DemoWriteReceiptRecord
import p2r_orchestration.report_jobs as report_jobs
import hashlib
import json
from datetime import datetime, timedelta, timezone
from p2r_orchestration.report_jobs import process_next_report_job
from p2r_persistence.models import EvaluationRecord, ExperimentRecord, MembershipRecord, PaperRecord, ProjectRecord, RunRecord, SpecRevisionRecord, UserRecord, ReportRecord, ReportBuildRecord, Job

@pytest.fixture
def seeded(tmp_path):
    store = LocalArtifactStore(tmp_path / 'artifacts')
    address = store.put_bytes('受控教学 fixture'.encode())
    app=create_app(db_url=f"sqlite:///{tmp_path/'report.sqlite3'}", supervisor=object(), secure_cookies=False, artifact_store=store)
    f=app.state.runtime['session_factory']
    with f.begin() as db:
        u=UserRecord(id='owner',display_name='owner',can_create_project=True); db.add(u); db.flush()
        p=ProjectRecord(name='p',created_by=u.id); db.add(p); db.flush(); db.add(MembershipRecord(project_id=p.id,user_id=u.id,role='owner'))
        paper=PaperRecord(project_id=p.id,sha256=address.sha256,artifact_key=address.sha256,bytes=address.bytes,page_count=0,source_kind='text_fixture',media_type='text/plain',status='ready'); db.add(paper); db.flush()
        from p2r_persistence.models import ExtractionRecord
        ext=ExtractionRecord(paper_id=paper.id,content_sha256=address.sha256,payload_json={}); db.add(ext); db.flush()
        exp=ExperimentRecord(project_id=p.id,paper_id=paper.id,extraction_id=ext.id,candidate_experiment_id='c',recipe_id='offline_fixture',recipe_version='1'); db.add(exp); db.flush()
        import hashlib,json
        spec={'target':'fixture'}; sh=hashlib.sha256(json.dumps(spec,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        rev=SpecRevisionRecord(experiment_id=exp.id,revision_no=1,spec_hash=sh,policy_hash='d'*64,spec_json=spec); db.add(rev); db.flush()
        run=RunRecord(project_id=p.id,experiment_id=exp.id,revision_id=rev.id,status='completed',manifest_hash='e'*64); db.add(run); db.flush()
        ev={'accuracy':1.0,'correct':4,'total':4}; mh=hashlib.sha256(json.dumps(ev,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        run.metrics_hash = mh
        db.add(EvaluationRecord(run_id=run.id,metrics_json=ev,verdict='matched',metrics_hash=mh,evaluator_version=EVALUATOR_VERSION))
        ids={'run':run.id,'project':p.id,'paper':paper.id,'revision':rev.id,'experiment':exp.id,'extraction':ext.id}
    return app,ids

def h(key): return {'X-Actor-ID':'owner','Idempotency-Key':key}
def create(client, run, key='b1', body=None): return client.post(f'/api/v1/runs/{run}/report-builds',headers=h(key),json=body or {})

def test_report_lifecycle_and_reuse(seeded):
    app,ids=seeded
    with TestClient(app) as c:
        assert c.get(f"/api/v1/runs/{ids['run']}/report-status", headers={'X-Actor-ID':'owner'}).json()['data']['status'] is None
        first=create(c,ids['run']); assert first.status_code==202; bid=first.json()['data']['build_id']
        assert create(c,ids['run'],'b1').json()['data']==first.json()['data']
        with app.state.runtime['session_factory']() as db:
            job=db.scalar(select(Job).where(Job.payload_ref==bid)); assert job.status=='queued'
        assert process_next_report_job(app.state.runtime['session_factory'])
        ready=c.get(f"/api/v1/runs/{ids['run']}/report-status", headers={'X-Actor-ID':'owner'}).json()['data']; assert ready['status']=='ready'; report=c.get(f"/api/v1/runs/{ids['run']}/report", headers={'X-Actor-ID':'owner'}).json()['data']
        again=create(c,ids['run'],'b2'); assert again.status_code==202; assert again.json()['data']['status']=='ready'
        assert c.get(f"/api/v1/runs/{ids['run']}/report", headers={'X-Actor-ID':'owner'}).json()['data']==report

def test_generating_visible_and_inputs_frozen(seeded, monkeypatch):
    app, ids = seeded
    factory = app.state.runtime['session_factory']
    original = report_jobs.build_report
    with TestClient(app) as client:
        for _ in range(2):
            assert client.get(f"/api/v1/runs/{ids['run']}/report-status", headers=h('r')).json()['data']['status'] is None
        with factory() as db:
            assert db.scalar(select(Job)) is None
        created = create(client, ids['run']).json()['data']
        with factory.begin() as db:
            evaluation = db.scalar(select(EvaluationRecord))
            evaluation.metrics_json = {'secret': 'secret-canary'}
        def inspect(snapshot):
            with factory() as db:
                assert db.get(ReportBuildRecord, created['build_id']).status == 'generating'
                assert db.get(OperationRecord, created['operation_id']).status == 'running'
            status = client.get(f"/api/v1/runs/{ids['run']}/report-status", headers=h('r')).json()['data']
            assert status['status'] == 'generating'
            assert create(client, ids['run'], 'pending-reuse').json()['data']['status'] == 'generating'
            assert snapshot['evidence'] == []
            assert snapshot['source']['sha256']
            assert snapshot['revision']['spec_hash']
            assert snapshot['run_created_at']
            return original(snapshot)
        monkeypatch.setattr(report_jobs, 'build_report', inspect)
        assert process_next_report_job(factory)
        report = client.get(f"/api/v1/runs/{ids['run']}/report", headers=h('r')).json()['data']
        assert report['evidence'] == []
        assert 'secret-canary' not in json.dumps(report)


@pytest.mark.parametrize('failure', ['exception', 'input_hash'])
def test_failure_retry_preserves_audit(seeded, monkeypatch, failure):
    app, ids = seeded
    factory = app.state.runtime['session_factory']
    original = report_jobs.build_report
    with TestClient(app) as client:
        first = create(client, ids['run']).json()['data']
        if failure == 'input_hash':
            with factory.begin() as db:
                db.get(ReportBuildRecord, first['build_id']).input_hash = '0' * 64
        else:
            def fail(snapshot):
                raise RuntimeError('secret-canary')
            monkeypatch.setattr(report_jobs, 'build_report', fail)
        assert process_next_report_job(factory)
        with factory() as db:
            old = db.get(ReportBuildRecord, first['build_id'])
            assert old.status == 'failed'
            assert old.error_json == {'code': 'REPORT_BUILD_FAILED'}
            assert db.scalar(select(Job)).error == 'REPORT_BUILD_FAILED'
        assert create(client, ids['run'], 'implicit-retry').status_code == 409
        retry = create(client, ids['run'], 'retry', {'retry_of': first['build_id']})
        assert retry.status_code == 202
        assert retry.json()['data']['build_id'] != first['build_id']
        assert create(client, ids['run'], 'stale', {'retry_of': first['build_id']}).status_code == 409
        monkeypatch.setattr(report_jobs, 'build_report', original)
        assert process_next_report_job(factory)
        assert create(client, ids['run'], 'ready-retry', {'retry_of': first['build_id']}).status_code == 409
        with factory() as db:
            assert db.get(ReportBuildRecord, first['build_id']).status == 'failed'
            assert len(db.scalars(select(ReportRecord)).all()) == 1
            assert db.get(ReportBuildRecord, retry.json()['data']['build_id']).generation == 2


@pytest.mark.parametrize('fence', ['owner', 'token', 'active', 'repository'])
@pytest.mark.parametrize('fails', [False, True])
def test_lost_fence_never_publishes_success_or_failure(seeded, monkeypatch, fence, fails):
    app, ids = seeded
    factory = app.state.runtime['session_factory']
    original = report_jobs.build_report
    with TestClient(app) as client:
        created = create(client, ids['run']).json()['data']
        def interrupt(snapshot):
            if fence == 'repository':
                monkeypatch.setattr(JobRepository, 'fail' if fails else 'complete', lambda *a, **k: False)
            else:
                with factory.begin() as db:
                    job = db.scalar(select(Job).where(Job.kind == 'report_build'))
                    if fence == 'owner':
                        job.leased_by = 'replacement'
                    elif fence == 'token':
                        job.fencing_token += 1
                    else:
                        db.get(RunRecord, ids['run']).active_report_build_id = None
            if fails:
                raise ValueError('secret-canary')
            return original(snapshot)
        monkeypatch.setattr(report_jobs, 'build_report', interrupt)
        assert process_next_report_job(factory) is False
        with factory() as db:
            assert db.get(ReportBuildRecord, created['build_id']).status == 'generating'
            assert db.get(OperationRecord, created['operation_id']).status == 'running'
            assert db.get(RunRecord, ids['run']).report_hash is None
            assert db.scalar(select(ReportRecord)) is None
            assert db.scalar(select(Job)).error is None


@pytest.mark.parametrize('fence', ['owner', 'token'])
def test_stale_claim_cannot_publish_generating(seeded, monkeypatch, fence):
    app, ids = seeded
    factory = app.state.runtime['session_factory']
    with TestClient(app) as client:
        created = create(client, ids['run']).json()['data']
        with factory.begin() as db:
            job = JobRepository(db).claim(worker_id='original', kinds=('report_build',))
            job_id, token = job.id, job.fencing_token
            if fence == 'owner':
                job.leased_by = 'replacement'
            else:
                job.fencing_token += 1
            before_build = db.get(ReportBuildRecord, created['build_id']).status
            before_operation = db.get(OperationRecord, created['operation_id']).status
        calls = []
        monkeypatch.setattr(report_jobs, 'build_report', lambda snapshot: calls.append(snapshot))
        assert report_jobs.process_claimed_report_job(
            factory, build_id=created['build_id'], job_id=job_id,
            worker_id='original', token=token,
        ) is False
        with factory() as db:
            assert db.get(ReportBuildRecord, created['build_id']).status == before_build
            assert db.get(OperationRecord, created['operation_id']).status == before_operation
        assert calls == []


def test_expired_claim_resumes_same_build_and_mixed_queue(seeded):
    app, ids = seeded
    factory = app.state.runtime['session_factory']
    with factory.begin() as db:
        other = JobRepository(db).enqueue(kind='unrelated', payload_ref='other')
        other_id = other.id
    with TestClient(app) as client:
        created = create(client, ids['run']).json()['data']
        with factory.begin() as db:
            job = JobRepository(db).claim(worker_id='interrupted', kinds=('report_build',))
            build = db.get(ReportBuildRecord, created['build_id'])
            build.status = 'generating'
            job.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
            job_id = job.id
        with factory.begin() as db:
            assert JobRepository(db).requeue_expired() == 1
        assert process_next_report_job(factory)
        assert process_next_report_job(factory) is False
        with factory() as db:
            assert db.get(Job, other_id).status == 'queued'
            assert db.get(Job, job_id).payload_ref == created['build_id']
            assert db.get(Job, job_id).attempt == 2
            assert len(db.scalars(select(ReportRecord)).all()) == 1


@pytest.mark.parametrize('corrupt', ['spec', 'revision_link', 'metrics', 'run_metrics', 'source', 'extraction'])
def test_snapshot_rejects_corrupt_inputs_atomically(seeded, corrupt):
    app, ids = seeded
    factory = app.state.runtime['session_factory']
    with factory.begin() as db:
        if corrupt == 'spec':
            db.get(SpecRevisionRecord, ids['revision']).spec_json = {'secret': 'secret-canary'}
        elif corrupt == 'revision_link':
            other = ExperimentRecord(project_id=ids['project'], paper_id=ids['paper'], extraction_id=ids['extraction'], candidate_experiment_id='other', recipe_id='offline_fixture', recipe_version='1')
            db.add(other); db.flush()
            db.get(SpecRevisionRecord, ids['revision']).experiment_id = other.id
        elif corrupt == 'metrics':
            db.scalar(select(EvaluationRecord)).metrics_hash = '0' * 64
        elif corrupt == 'run_metrics':
            db.get(RunRecord, ids['run']).metrics_hash = '0' * 64
        elif corrupt == 'source':
            db.get(PaperRecord, ids['paper']).sha256 = '0' * 64
        else:
            from p2r_persistence.models import ExtractionRecord
            db.get(ExtractionRecord, ids['extraction']).content_sha256 = '0' * 64
    with TestClient(app) as client:
        response = create(client, ids['run'])
        assert response.status_code == 409
        assert 'secret-canary' not in response.text
    with factory() as db:
        assert db.scalar(select(ReportBuildRecord)) is None
        assert db.scalar(select(OperationRecord)) is None
        assert db.scalar(select(Job)) is None


@pytest.mark.parametrize('kind', ['paper_ingestion', 'paper_extraction', 'report_build', 'export_bundle', 'unknown'])
def test_operations_route_and_permission_matrix(seeded, kind):
    app, ids = seeded
    factory = app.state.runtime['session_factory']
    with factory.begin() as db:
        resource = ids['paper'] if kind.startswith('paper_') or kind == 'unknown' else ids['run']
        if kind == 'export_bundle':
            export = ExportRecord(run_id=ids['run'], requested_formats_json=['report_json'], status='queued')
            db.add(export); db.flush()
            resource = export.id
        op = OperationRecord(kind=kind, resource_id=resource, status='queued')
        db.add(op); db.flush()
        op_id = op.id
    with TestClient(app) as client:
        response = client.get(f'/api/v1/operations/{op_id}', headers=h('r'))
        assert response.status_code == (404 if kind == 'unknown' else 200)
        if kind != 'unknown':
            assert response.json()['data']['kind'] == kind
        with factory.begin() as db:
            db.delete(db.scalar(select(MembershipRecord)))
        assert client.get(f'/api/v1/operations/{op_id}', headers=h('r')).status_code == 404


def test_demo_receipt_and_authorization_before_replay(seeded):
    app, ids = seeded
    app.state.runtime['demo_enabled'] = True
    factory = app.state.runtime['session_factory']
    with factory.begin() as db:
        db.add(UserRecord(id='demo-user', display_name='demo'))
        db.flush()
        db.add(MembershipRecord(project_id=ids['project'], user_id='demo-user', role='owner'))
    with TestClient(app) as client:
        path = f"/api/v1/runs/{ids['run']}/report-builds"
        headers = {'X-Actor-ID': 'demo-user', 'Idempotency-Key': 'b1'}
        first = client.post(path, headers=headers, json={})
        assert first.status_code == 202
        with factory() as db:
            receipt = db.get(DemoWriteReceiptRecord, ('demo-user', 'b1'))
            assert receipt.response_json == first.json()['data']
        assert client.post(path, headers=headers, json={}).json()['data'] == first.json()['data']
        with factory.begin() as db:
            db.delete(db.scalar(select(MembershipRecord).where(MembershipRecord.user_id == 'demo-user')))
        assert client.post(path, headers=headers, json={}).status_code == 404


def test_concurrent_requests_reserve_one_active_generation(seeded):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    app, ids = seeded
    barrier = Barrier(2)
    def post(key):
        with TestClient(app) as client:
            barrier.wait(timeout=5)
            response = create(client, ids['run'], key)
            assert response.status_code == 202, response.text
            return response.json()['data']
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(post, ['one', 'two']))
    assert responses[0]['build_id'] == responses[1]['build_id']
    with app.state.runtime['session_factory']() as db:
        for model in (ReportBuildRecord, OperationRecord, Job):
            assert len(db.scalars(select(model)).all()) == 1


def test_receipt_failure_rolls_back_build_slot_and_queue(seeded, monkeypatch):
    import p2r_api.routes.reports as reports
    app, ids = seeded
    def fail(*args, **kwargs):
        raise RuntimeError('receipt write interrupted')
    monkeypatch.setattr(reports, 'receipt_save', fail)
    with TestClient(app, raise_server_exceptions=False) as client:
        assert create(client, ids['run']).status_code == 500
    with app.state.runtime['session_factory']() as db:
        assert db.get(RunRecord, ids['run']).active_report_build_id is None
        for model in (ReportBuildRecord, OperationRecord, Job):
            assert db.scalar(select(model)) is None


def test_duplicate_delivery_and_existing_report_are_immutable(seeded):
    app, ids = seeded
    factory = app.state.runtime['session_factory']
    with TestClient(app) as client:
        created = create(client, ids['run']).json()['data']
        assert process_next_report_job(factory)
        with factory.begin() as db:
            report = db.scalar(select(ReportRecord))
            content, content_hash = report.content_json, report.content_hash
            JobRepository(db).enqueue(kind='report_build', payload_ref=created['build_id'])
        assert process_next_report_job(factory)
        with factory() as db:
            assert db.get(ReportBuildRecord, created['build_id']).status == 'ready'
            assert db.get(RunRecord, ids['run']).report_hash == content_hash
            reports = db.scalars(select(ReportRecord)).all()
            assert len(reports) == 1 and reports[0].content_json == content


def test_legacy_report_ready_without_synthetic_operation(seeded):
    app, ids = seeded
    factory = app.state.runtime['session_factory']
    with factory.begin() as db:
        db.add(ReportRecord(run_id=ids['run'], content_json={'legacy': True}, content_hash='a' * 64))
    with TestClient(app) as client:
        status = client.get(f"/api/v1/runs/{ids['run']}/report-status", headers=h('r')).json()['data']
        assert status['status'] == 'ready'
        assert status['build_id'] is None and status['operation_id'] is None
        assert create(client, ids['run']).json()['data']['status'] == 'ready'
    with factory() as db:
        assert db.scalar(select(Job)) is None
        assert db.scalar(select(OperationRecord)) is None


@pytest.mark.parametrize('body', [{'retry_of': None}, {'retry_of': 1}, {'retry_of': {}}, {'retry_of': ''}, {'extra': 1}])
def test_report_build_body_is_strict(seeded, body):
    app, ids = seeded
    with TestClient(app) as client:
        assert create(client, ids['run'], body=body).status_code == 422


def test_workers_do_not_claim_each_others_jobs(seeded):
    from p2r_orchestration.run_worker import process_next_run_job
    app, ids = seeded
    factory = app.state.runtime['session_factory']
    with TestClient(app) as client:
        create(client, ids['run'])
        assert process_next_run_job(factory) is False
        with factory.begin() as db:
            assert JobRepository(db).claim(worker_id='none', kinds=()) is None
            unrelated = JobRepository(db).enqueue(kind='run_fixture', payload_ref=ids['run'])
            unrelated_id = unrelated.id
        assert process_next_report_job(factory)
        with factory() as db:
            assert db.get(Job, unrelated_id).status == 'queued'


def test_report_requires_completed_and_idempotency(seeded):
    app,ids=seeded
    with app.state.runtime['session_factory'].begin() as db: db.get(RunRecord,ids['run']).status='running'
    with TestClient(app) as c:
        assert c.post(f'/api/v1/runs/{ids["run"]}/report-builds',headers={'X-Actor-ID':'owner'},json={}).status_code==422
        assert c.post(f'/api/v1/runs/{ids["run"]}/report-builds',headers=h('x'),json={}).status_code==409
