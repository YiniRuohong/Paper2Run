"""Server-owned teaching fixture, explicitly not a research reproduction."""
from __future__ import annotations

import threading

from p2r_persistence.job_repository import JobRepository

SOURCE = b'Paper2Run teaching fixture.\nThis is a controlled smoke check of the offline fixture recipe.\nIt is not a research paper and not a full reproduction.\n'
FIXTURE = {'id': 'offline-smoke-v1', 'title': 'Paper2Run teaching fixture',
           'source_kind': 'text_fixture', 'description': 'Offline controlled smoke check; not a research paper.'}
LABELS = ['local demo', 'controlled smoke', 'not full reproduction']
CANDIDATE = {'id': 'offline-smoke', 'recipe_id': 'offline_fixture', 'recipe_version': '1',
             'label': 'Controlled smoke', 'execution_support': 'supported',
             'provenance': 'manually_configured_recipe'}

DEMO_JOB_KINDS = ('run_fixture', 'report_build', 'export_bundle', 'export_report_json')
_WORKER_ID = 'guided-demo'
_STOP_TIMEOUT_SECONDS = 2.0


def recover_expired_demo_leases(session_factory) -> int:
    """At demo startup only, requeue expired leases for supported local jobs.

    Active leases and unrelated job kinds are never preempted.
    """
    with session_factory() as db:
        recovered = JobRepository(db).requeue_expired(kinds=DEMO_JOB_KINDS)
        db.commit()
        return recovered


def process_next_demo_job(session_factory, artifact_store=None, *, worker_id=_WORKER_ID) -> bool:
    """Consume exactly one guided-demo job and dispatch a fixed handler.

    The worker claims only the four supported kinds, so it cannot take and
    fail a Session/repair/execution job owned by another worker.
    """
    with session_factory() as db:
        connection = db.connection()
        if connection.dialect.name == 'sqlite' and not connection.connection.driver_connection.in_transaction:
            connection.exec_driver_sql('BEGIN IMMEDIATE')
        job = JobRepository(db).claim(worker_id=worker_id, kinds=DEMO_JOB_KINDS)
        if job is None:
            db.commit()
            return False
        job_id, token, kind, payload_ref = job.id, job.fencing_token, job.kind, job.payload_ref
        db.commit()

    try:
        if kind == 'run_fixture':
            handled = _process_run_fixture(session_factory, artifact_store=artifact_store,
                                           worker_id=worker_id, job_id=job_id,
                                           payload_ref=payload_ref, token=token)
        elif kind == 'report_build':
            # The initial claim above is the only claim for this job; the
            # handler acts on that claim and never races a second worker.
            handled = _process_owned_report(session_factory, worker_id=worker_id, job_id=job_id,
                                            payload_ref=payload_ref, token=token)
        elif kind in {'export_bundle', 'export_report_json'}:
            handled = _process_owned_export(session_factory, artifact_store, worker_id=worker_id,
                                            job_id=job_id, payload_ref=payload_ref, token=token)
        else:
            handled = False
    except Exception:
        # Handler implementations already persist fixed public errors; this
        # boundary only prevents raw exception text from escaping the demo
        # worker loop.
        _fail_owned_job(session_factory, job_id, worker_id=worker_id, token=token, kind=kind,
                        payload_ref=payload_ref)
        return True
    return bool(handled)


def _process_run_fixture(session_factory, *, artifact_store, worker_id, job_id, payload_ref, token):
    from p2r_orchestration.run_worker import process_claimed_run_fixture

    return process_claimed_run_fixture(
        session_factory, job_id=job_id, payload_ref=payload_ref, worker_id=worker_id, token=token,
        artifact_store=artifact_store,
    )


def _process_owned_report(session_factory, *, worker_id, job_id, payload_ref, token):
    from p2r_orchestration import report_jobs

    return report_jobs.process_claimed_report_job(
        session_factory, job_id=job_id, build_id=payload_ref, worker_id=worker_id, token=token,
    )


def _process_owned_export(session_factory, artifact_store, *, worker_id, job_id, payload_ref, token):
    from p2r_orchestration import export_jobs

    if artifact_store is None:
        _fail_owned_job(session_factory, job_id, worker_id=worker_id, token=token,
                        kind='export_bundle', payload_ref=payload_ref, public_error='export failed')
        return True
    return export_jobs.process_claimed_export_job(
        session_factory, artifact_store, job_id=job_id, export_id=payload_ref,
        worker_id=worker_id, token=token,
    )


def _fail_owned_job(session_factory, job_id, *, worker_id, token, kind, payload_ref,
                    public_error='PIPELINE_FAILED'):
    from sqlalchemy import select
    from p2r_persistence.models import (
        ExecutionRecord, ExportItemRecord, ExportRecord, OperationRecord,
        ReportBuildRecord, RunRecord, utcnow,
    )

    with session_factory() as db:
        if not JobRepository(db).fail(job_id, worker_id=worker_id, fencing_token=token, error=public_error):
            db.rollback()
            return False
        operation_id = None
        code = 'PIPELINE_FAILED'
        if kind == 'run_fixture':
            run = db.get(RunRecord, payload_ref)
            if run is not None and run.status in {'queued', 'running', 'evaluating', 'cancel_requested'}:
                from p2r_orchestration.run_worker import _event
                run.status = 'failed'
                run.error_json = {'code': code, 'message': 'The controlled pipeline failed.'}
                for execution in db.scalars(select(ExecutionRecord).where(
                        ExecutionRecord.job_id == job_id, ExecutionRecord.runner_id == worker_id,
                        ExecutionRecord.fencing_token == token, ExecutionRecord.status == 'running')):
                    execution.status = 'failed'
                    execution.error_json = dict(run.error_json)
                    execution.finished_at = utcnow()
                    execution.lease_until = None
                _event(db, run.id, 'run.terminal', {'status': 'failed'})
        elif kind == 'report_build':
            code = 'REPORT_BUILD_FAILED'
            build = db.get(ReportBuildRecord, payload_ref)
            run = db.get(RunRecord, build.run_id) if build else None
            if build is not None and run is not None and run.active_report_build_id == build.id and build.status in {'pending', 'generating'}:
                build.status = 'failed'
                build.error_json = {'code': code}
                operation_id = build.operation_id
        elif kind in {'export_bundle', 'export_report_json'}:
            code = 'EXPORT_FAILED'
            export = db.get(ExportRecord, payload_ref)
            if export is not None and export.status in {'queued', 'running'}:
                from p2r_orchestration.export_jobs import _set_projection
                items = db.scalars(select(ExportItemRecord).where(ExportItemRecord.export_id == export.id)).all()
                for item in items:
                    if item.status in {'pending', 'running'}:
                        item.status = 'failed'
                        item.error_json = {'code': code}
                _set_projection(export, items)
                export.error_json = {'code': code}
                operation_id = export.operation_id
        if operation_id:
            operation = db.get(OperationRecord, operation_id)
            if operation is not None and operation.status in {'queued', 'running'}:
                operation.status = 'failed'
                operation.error_json = {'code': code}
        db.commit()
        return True


def start_demo_worker(session_factory, artifact_store):
    stop = threading.Event()
    recover_expired_demo_leases(session_factory)

    def run():
        while not stop.is_set():
            worked = process_next_demo_job(session_factory, artifact_store, worker_id=_WORKER_ID)
            if not worked:
                stop.wait(0.2)

    thread = threading.Thread(target=run, name='guided-demo-worker', daemon=False)
    thread.start()
    return stop, thread


def stop_demo_worker(stop, thread, timeout=_STOP_TIMEOUT_SECONDS):
    if stop is not None:
        stop.set()
    if thread is not None:
        thread.join(timeout=timeout)
    return not thread.is_alive() if thread is not None else True


def extraction_payload(paper_id, extraction_id, digest):
    return {'extraction_id': extraction_id, 'paper_id': paper_id, 'schema_version': '1.0',
            'fields': {}, 'methods': [], 'formulas': [], 'candidate_experiments': [CANDIDATE],
            'warnings': LABELS, 'source_refs': [{
                'source_kind': 'text_fixture', 'source_sha256': digest,
                'quote': SOURCE.decode('utf-8'), 'byte_start': 0, 'byte_end': len(SOURCE), 'page': None,
            }]}
