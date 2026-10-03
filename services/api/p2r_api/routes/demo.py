from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, StrictBool, field_validator
from p2r_persistence.models import (
    DemoWriteReceiptRecord, ExtractionRecord, MembershipRecord, PaperRecord,
    PaperVersionRecord, ProjectRecord, UserRecord,
)
from ..auth import require_project_access
from ..dependencies import actor_id
from ..demo import FIXTURE, LABELS, SOURCE, extraction_payload
from ..demo_receipts import error, is_allowed_path, lookup, save

router = APIRouter()


def envelope(request, data):
    return {'data': data, 'meta': {'request_id': request.state.request_id}}


class Bootstrap(BaseModel):
    model_config = ConfigDict(extra='forbid')
    fixture_id: Literal['offline-smoke-v1']
    source_processing_consent: StrictBool

    @field_validator('source_processing_consent')
    @classmethod
    def consent_required(cls, value):
        if value is not True:
            raise ValueError('source_processing_consent must be true')
        return value


@router.get('/demo')
def get_demo(request: Request):
    return envelope(request, {'actor_id': actor_id(request), 'fixture': FIXTURE, 'labels': LABELS})


@router.post('/demo/bootstrap', status_code=201)
async def bootstrap(request: Request, body: Bootstrap):
    uid = actor_id(request)
    raw = await request.body()
    runtime = request.app.state.runtime
    with runtime['session_factory']() as db:
        cached = lookup(request, db, raw)
        if cached is not None:
            return envelope(request, cached)
        if db.get(UserRecord, uid) is None:
            db.add(UserRecord(id=uid, display_name='Local demo', can_create_project=True))
            db.flush()
        project = ProjectRecord(name='Controlled smoke', created_by=uid)
        db.add(project)
        db.flush()
        db.add(MembershipRecord(project_id=project.id, user_id=uid, role='owner'))
        address = runtime['artifact_store'].put_bytes(SOURCE)
        metadata = dict(sha256=address.sha256, artifact_key=address.sha256, bytes=len(SOURCE),
                        page_count=0, source_kind='text_fixture', media_type='text/plain')
        paper = PaperRecord(project_id=project.id, title=FIXTURE['title'], status='ready',
                            processing_consent=True, **metadata)
        db.add(paper)
        db.flush()
        db.add(PaperVersionRecord(paper_id=paper.id, version=1, **metadata))
        extraction = ExtractionRecord(paper_id=paper.id, content_sha256=address.sha256, status='ready')
        db.add(extraction)
        db.flush()
        extraction.payload_json = extraction_payload(paper.id, extraction.id, address.sha256)
        out = {'project_id': project.id, 'paper_id': paper.id, 'extraction_id': extraction.id}
        save(request, db, raw, project.id, out)
        db.commit()
        return envelope(request, out)


@router.get('/demo/write-receipts/{key}')
def get_receipt(key: str, request: Request):
    uid = actor_id(request)
    with request.app.state.runtime['session_factory']() as db:
        row = db.get(DemoWriteReceiptRecord, (uid, key))
        if row is None or row.project_id is None or not is_allowed_path(row.target_path):
            error('RESOURCE_NOT_FOUND', 'resource not found', 404)
        require_project_access(db, uid, row.project_id)
        return envelope(request, {'state': 'completed', 'target_path': row.target_path,
                                  'request_sha256': row.request_sha256, 'response': row.response_json})
