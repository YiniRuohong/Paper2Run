from __future__ import annotations
from datetime import datetime
from fastapi import APIRouter, Request, Response
from pydantic import BaseModel
from sqlalchemy import select
from p2r_persistence.models import MembershipRecord, UserRecord
from ..auth import IdentityService, SessionRequiredError, CsrfError, validate_origin, validate_csrf, csrf_token
from starlette.responses import Response as StarletteResponse
router = APIRouter()
class _User(BaseModel): id: str; display_name: str
class _Times(BaseModel): expires_at: datetime; idle_expires_at: datetime
class _Perm(BaseModel): can_create_project: bool
class _Member(BaseModel): project_id: str; role: str
class SessionInfo(BaseModel):
    user: _User; session: _Times; csrf_token: str; permissions: _Perm; memberships: list[_Member]
def session_info(db, auth, secret):
    user=db.get(UserRecord,auth.user_id)
    return SessionInfo(user=_User(id=user.id,display_name=user.display_name),session=_Times(expires_at=auth.expires_at,idle_expires_at=auth.idle_expires_at),csrf_token=csrf_token(secret),permissions=_Perm(can_create_project=user.can_create_project),memberships=[_Member(project_id=x.project_id,role=x.role) for x in db.scalars(select(MembershipRecord).where(MembershipRecord.user_id==user.id))])
def set_session_cookie(response,secret,*,secure=True): response.headers["Cache-Control"]="no-store"; response.set_cookie("p2r_session",secret,max_age=43200,httponly=True,secure=secure,samesite="lax",path="/")
def clear_session_cookie(response,*,secure=True): response.headers["Cache-Control"]="no-store"; response.delete_cookie("p2r_session",httponly=True,secure=secure,samesite="lax",path="/")
def _db(request): return request.app.state.runtime["session_factory"]()
def _origin(request):
    allowed=request.app.state.runtime.get("allowed_origin")
    if not allowed: raise CsrfError("origin rejected")
    validate_origin(request.headers.get("origin"),allowed)
def _envelope(request,data): return {"data":data,"meta":{"request_id":getattr(request.state,"request_id","")}}
@router.get("/auth/me")
def me(request:Request):
    with _db(request) as db:
        secret=request.cookies.get("p2r_session"); service=IdentityService(db); row=service.get_current_session(secret); payload=session_info(db,row,secret).model_dump(mode="json")
    return _envelope(request,payload)
@router.delete("/auth/session", status_code=204, response_class=Response)
def logout(request:Request,response:Response):
    _origin(request); secret=request.cookies.get("p2r_session")
    with _db(request) as db:
        service=IdentityService(db); service.get_current_session(secret); validate_csrf(request.headers.get("x-csrf-token"),csrf_token(secret)); service.revoke_session(secret)
    clear_session_cookie(response,secure=request.app.state.runtime["secure_cookies"]); response.headers["Cache-Control"]="no-store"; response.status_code = 204; return response
