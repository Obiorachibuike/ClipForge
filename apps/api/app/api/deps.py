"""Request dependencies: authentication, CSRF, rate limiting, ownership."""
from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Cookie, Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.core.errors import AuthError, NotFoundError, PermissionError_, RateLimitedError
from app.core.logging import get_logger
from app.core.rate_limit import check_rate_limit
from app.core.security import csrf_matches, decode_session_token, token_fingerprint
from app.models import Clip, Job, Project, RenderJob, Session as SessionModel, User, Video

log = get_logger(__name__)

DbSession = Annotated[Session, Depends(get_db)]

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_last_seen_write: dict[str, float] = {}


def client_identity(request: Request) -> str:
    """Identity for rate limiting: authenticated user when known, else IP."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _load_session(request: Request, db: Session) -> tuple[SessionModel, User] | None:
    token = request.cookies.get(settings.session_cookie_name)
    if not token:
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            token = header[7:].strip()
    if not token:
        return None
    payload = decode_session_token(token)
    session = db.execute(
        select(SessionModel).where(SessionModel.token_hash == token_fingerprint(token))
    ).scalars().first()
    if session is None or session.revoked_at is not None:
        raise AuthError("Your session is no longer valid. Please sign in again.", code="invalid_session")
    expires = session.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    if expires < datetime.now(UTC):
        raise AuthError("Your session has expired. Please sign in again.", code="session_expired")
    user = db.get(User, session.user_id)
    if user is None or not user.is_active:
        raise AuthError("This account is not active.", code="account_inactive")
    if payload.get("sid") != session.id:
        raise AuthError("Your session is invalid.", code="invalid_session")
    # Cheap last-seen bookkeeping, throttled to once a minute per session.
    now = time.time()
    if now - _last_seen_write.get(session.id, 0) > 60:
        session.last_seen_at = datetime.now(UTC)
        user.last_login_at = user.last_login_at or datetime.now(UTC)
        db.commit()
        _last_seen_write[session.id] = now
    return session, user


def get_current_user(request: Request, db: DbSession) -> User:
    loaded = _load_session(request, db)
    if loaded is None:
        raise AuthError()
    _, user = loaded
    _enforce_csrf(request, loaded[0])
    return user


def get_optional_user(request: Request, db: DbSession) -> User | None:
    loaded = _load_session(request, db)
    return loaded[1] if loaded else None


def _enforce_csrf(request: Request, session: SessionModel) -> None:
    """Double-submit CSRF token check for state-changing requests."""
    if request.method in SAFE_METHODS:
        return
    if request.url.path.endswith("/auth/login") or request.url.path.endswith("/auth/register"):
        return
    cookie_token = request.cookies.get(settings.csrf_cookie_name)
    header_token = request.headers.get(settings.csrf_header_name)
    if not session.csrf_token:
        return
    if not csrf_matches(session.csrf_token, header_token) and not csrf_matches(session.csrf_token, cookie_token):
        raise AuthError("Security check failed. Refresh the page and try again.", code="csrf_failed")


CurrentUser = Annotated[User, Depends(get_current_user)]
OptionalUser = Annotated[User | None, Depends(get_optional_user)]


# ------------------------------------------------------------ rate limiting ---
def rate_limit(bucket: str, limit: int | None = None, window: int | None = None):
    """Dependency factory: limiter per identity per bucket."""

    def _dependency(request: Request) -> None:
        check_rate_limit(
            client_identity(request),
            bucket,
            limit or settings.rate_limit_requests,
            window or settings.rate_limit_window_seconds,
        )

    return _dependency


def upload_rate_limit(request: Request, user: OptionalUser = None) -> None:
    identity = user.id if user else client_identity(request)
    check_rate_limit(identity, "upload", settings.rate_limit_upload_requests, settings.rate_limit_window_seconds)


def auth_rate_limit(request: Request) -> None:
    check_rate_limit(client_identity(request), "auth", settings.rate_limit_auth_requests, 300)


# --------------------------------------------------------------- ownership ---
def owned_project(db: Session, user: User, project_id: str, *, allow_archived: bool = True) -> Project:
    project = db.get(Project, project_id)
    if project is None or project.user_id != user.id:
        raise NotFoundError("Project not found.", code="project_not_found")
    if not allow_archived and project.archived_at is not None:
        raise NotFoundError("Project not found.", code="project_not_found")
    return project


def owned_video(db: Session, user: User, video_id: str) -> Video:
    video = db.get(Video, video_id)
    if video is None or video.user_id != user.id:
        raise NotFoundError("Video not found.", code="video_not_found")
    return video


def owned_clip(db: Session, user: User, clip_id: str) -> Clip:
    clip = db.get(Clip, clip_id)
    if clip is None or clip.user_id != user.id:
        raise NotFoundError("Clip not found.", code="clip_not_found")
    return clip


def owned_render(db: Session, user: User, render_id: str) -> RenderJob:
    render = db.get(RenderJob, render_id)
    if render is None or render.user_id != user.id:
        raise NotFoundError("Render not found.", code="render_not_found")
    return render


def owned_job(db: Session, user: User, job_id: str) -> Job:
    job = db.get(Job, job_id)
    if job is None or (job.user_id is not None and job.user_id != user.id):
        raise NotFoundError("Job not found.", code="job_not_found")
    return job


def require_admin(user: CurrentUser) -> User:
    if not user.is_admin:
        raise PermissionError_("Administrator access is required.")
    return user


# ----------------------------------------------------------------- helpers ---
def pagination(limit: int = 50, offset: int = 0) -> tuple[int, int]:
    return max(1, min(200, limit)), max(0, offset)


class PaginationParams:
    def __init__(self, limit: int = 50, offset: int = 0) -> None:
        self.limit, self.offset = pagination(limit, offset)


PageParams = Annotated[PaginationParams, Depends(PaginationParams)]


def request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


def csrf_header_optional(x_csrf_token: str | None = Header(default=None)) -> str | None:
    return x_csrf_token


def session_cookie(request: Request, clipforge_session: str | None = Cookie(default=None)) -> str | None:
    return clipforge_session or request.cookies.get(settings.session_cookie_name)
