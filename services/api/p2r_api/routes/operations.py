from fastapi import APIRouter, Request
from sqlalchemy import select
from p2r_persistence.models import ExportRecord, OperationRecord, PaperRecord, RunRecord
from p2r_orchestration.service import ServiceError
from ..auth import require_project_access
from ..dependencies import actor_id

router=APIRouter()

def error(code,message,status=422):
    ex=ServiceError(message); ex.code=code; ex.status=status; raise ex

@router.get('/operations/{operation_id}')
def get_operation(operation_id: str, request: Request):
    with request.app.state.runtime['session_factory']() as db:
        op=db.get(OperationRecord, operation_id)
        if op is None: error('RESOURCE_NOT_FOUND','resource not found',404)
        project_id=None
        if op.kind in {'paper_ingestion','paper_extraction'}:
            paper=db.get(PaperRecord,op.resource_id); project_id=paper.project_id if paper else None
        elif op.kind=='report_build':
            run=db.get(RunRecord,op.resource_id); project_id=run.project_id if run else None
        elif op.kind in {'export_bundle','export_report_json'}:
            export=db.get(ExportRecord,op.resource_id); run=db.get(RunRecord,export.run_id) if export else None; project_id=run.project_id if run else None
        else:
            error('RESOURCE_NOT_FOUND','resource not found',404)
        if project_id is None: error('RESOURCE_NOT_FOUND','resource not found',404)
        require_project_access(db,actor_id(request),project_id)
        return {'data': {'operation_id':op.id,'kind':op.kind,'resource_id':op.resource_id,'status':op.status,'error':op.error_json}, 'meta': {'request_id':request.state.request_id}}
