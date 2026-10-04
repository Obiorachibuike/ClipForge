"""Authentication endpoints.

Sessions are HTTP-only cookies; the CSRF token is returned to the client and
echoed back in a header on state-changing requests. Tokens are never handed to
JavaScript, so localStorage never holds a credential.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession, auth_rate_limit, client_identity
from app.core.config import settings
from app.core.errors import AuthError, ConflictError, ValidationError
from app.core.logging import get_logger
from app.core.security import (
    hash_password,
    new_csrf_token,
    new_reset_token,
    session_expiry,
    token_fingerprint,
    verify_password,
)
from app.models import PasswordResetToken, Session as SessionModel, User, as_utc, utcnow
from app.schemas.auth import (
    Credentials,
    PasswordChange,
    PasswordResetConfirm,
    PasswordResetRequest,
    RegisterRequest,
    SessionOut,
    UserOut,
)
from app.schemas.common import MessageResponse, OkResponse
from app.services.billing.subscription_service import SubscriptionService

log = get_logger(__name__)
router = APIRouter(prefix="/auth", tags=["Authentication"])


def _set_session_cookies(response: Response, token: str, csrf: str, expires_at: datetime) -> None:
    max_age = max(60, int((as_utc(expires_at) - datetime.now(UTC)).total_seconds()))
    response.set_cookie(
        settings.session_cookie_name,
        token,
        max_age=max_age,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        domain=settings.cookie_domain,
        path="/",
    )
    # Readable by JS on purpose: this is the double-submit CSRF token, not a credential.
    response.set_cookie(
        settings.csrf_cookie_name,
        csrf,
        max_age=max_age,
        httponly=False,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        domain=settings.cookie_domain,
        path="/",
    )


def _create_session(db: DbSession, user: User, request: Request, response: Response) -> SessionOut:
    session = SessionModel(
        user_id=user.id,
        token_hash="pending",
        csrf_token=new_csrf_token(),
        created_ip=client_identity(request),
        user_agent=(request.headers.get("user-agent") or "")[:400],
        expires_at=session_expiry(),
    )
    db.add(session)
    db.flush()
    from app.core.security import create_session_token

    token = create_session_token(user.id, session.id, session.expires_at)
    session.token_hash = token_fingerprint(token)
    user.last_login_at = utcnow()
    db.commit()
    _set_session_cookies(response, token, session.csrf_token, session.expires_at)
    SubscriptionService.get_or_create(db, user)
    return SessionOut(user=UserOut.model_validate(user), csrf_token=session.csrf_token, expires_at=session.expires_at)


@router.post("/register", response_model=SessionOut, status_code=status.HTTP_201_CREATED, dependencies=[Depends(auth_rate_limit)])
def register(request: Request, payload: RegisterRequest, response: Response, db: DbSession) -> SessionOut:
    email = payload.email.lower().strip()
    existing = db.execute(select(User).where(User.email == email)).scalars().first()
    if existing is not None:
        raise ConflictError("An account with that email already exists.", code="email_taken")
    user = User(
        email=email,
        hashed_password=hash_password(payload.password),
        name=payload.name or email.split("@")[0][:60],
        email_verified_at=None,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    log.info("auth.registered", user_id=user.id)
    return _create_session(db, user, request, response)


@router.post("/login", response_model=SessionOut, dependencies=[Depends(auth_rate_limit)])
def login(request: Request, payload: Credentials, response: Response, db: DbSession) -> SessionOut:
    email = payload.email.lower().strip()
    user = db.execute(select(User).where(User.email == email)).scalars().first()
    if user is None or not user.hashed_password or not verify_password(payload.password, user.hashed_password):
        # Identical error for unknown email and bad password.
        raise AuthError("Incorrect email or password.", code="invalid_credentials")
    if not user.is_active:
        raise AuthError("This account has been disabled.", code="account_inactive")
    log.info("auth.login", user_id=user.id)
    return _create_session(db, user, request, response)


@router.post("/logout", response_model=OkResponse)
def logout(request: Request, response: Response, db: DbSession, user: CurrentUser) -> OkResponse:
    token = request.cookies.get(settings.session_cookie_name) or ""
    if token:
        session = db.execute(
            select(SessionModel).where(SessionModel.token_hash == token_fingerprint(token))
        ).scalars().first()
        if session is not None:
            session.revoked_at = utcnow()
            db.commit()
    response.delete_cookie(settings.session_cookie_name, path="/", domain=settings.cookie_domain)
    response.delete_cookie(settings.csrf_cookie_name, path="/", domain=settings.cookie_domain)
    return OkResponse(message="Signed out.")


@router.get("/session", response_model=SessionOut)
def read_session(request: Request, response: Response, db: DbSession, user: CurrentUser) -> SessionOut:
    token = request.cookies.get(settings.session_cookie_name) or ""
    session = db.execute(
        select(SessionModel).where(SessionModel.token_hash == token_fingerprint(token))
    ).scalars().first()
    if session is None:
        raise AuthError()
    # Refresh the CSRF cookie so a page reload keeps working.
    _set_session_cookies(response, token, session.csrf_token, session.expires_at)
    return SessionOut(user=UserOut.model_validate(user), csrf_token=session.csrf_token, expires_at=session.expires_at)


@router.post("/password/reset-request", response_model=MessageResponse, dependencies=[Depends(auth_rate_limit)])
def request_password_reset(payload: PasswordResetRequest, db: DbSession) -> MessageResponse:
    email = payload.email.lower().strip()
    user = db.execute(select(User).where(User.email == email)).scalars().first()
    if user is not None:
        reset = PasswordResetToken(
            user_id=user.id,
            token_hash="pending",
            expires_at=utcnow().replace(microsecond=0) + timedelta(hours=2),
        )
        db.add(reset)
        db.flush()
        raw = new_reset_token()
        reset.token_hash = token_fingerprint(raw)
        db.commit()
        # Delivery is intentionally out of scope here: log the operator, who can
        # wire an email provider. The API never leaks whether the email exists.
        log.info("auth.password_reset_requested", user_id=user.id, token_hint=raw[:6])
    return MessageResponse(
        message="If an account exists for that email, password reset instructions have been sent."
    )


@router.post("/password/reset", response_model=OkResponse, dependencies=[Depends(auth_rate_limit)])
def confirm_password_reset(payload: PasswordResetConfirm, db: DbSession) -> OkResponse:
    record = db.execute(
        select(PasswordResetToken).where(PasswordResetToken.token_hash == token_fingerprint(payload.token))
    ).scalars().first()
    if record is None or record.used_at is not None:
        raise ValidationError("That reset link is not valid.", field="token")
    expires = as_utc(record.expires_at)
    if expires < datetime.now(UTC):
        raise ValidationError("That reset link has expired.", field="token")
    user = db.get(User, record.user_id)
    if user is None:
        raise ValidationError("That reset link is not valid.", field="token")
    user.hashed_password = hash_password(payload.password)
    record.used_at = utcnow()
    # Invalidate every existing session for this account.
    for session in db.execute(select(SessionModel).where(SessionModel.user_id == user.id)).scalars():
        session.revoked_at = utcnow()
    db.commit()
    log.info("auth.password_reset_completed", user_id=user.id)
    return OkResponse(message="Password updated. Please sign in.")


@router.post("/password/change", response_model=OkResponse)
def change_password(payload: PasswordChange, db: DbSession, user: CurrentUser) -> OkResponse:
    if not user.hashed_password or not verify_password(payload.current_password, user.hashed_password):
        raise AuthError("Your current password is incorrect.", code="invalid_credentials")
    user.hashed_password = hash_password(payload.new_password)
    db.commit()
    return OkResponse(message="Password updated.")
