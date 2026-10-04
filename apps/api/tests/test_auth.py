"""Authentication, session and CSRF behaviour."""
from __future__ import annotations

import secrets

from app.core.config import settings


def _new_password() -> str:
    """Build a valid random test credential; no password literal is stored."""
    return f"Test{secrets.token_hex(24)}7"


def test_register_creates_session_cookie_and_never_returns_a_token(api):
    user = api.register(email="alice@example.com")

    assert user["email"] == "alice@example.com"
    assert user["plan"] == "free"
    assert "password" not in user and "hashed_password" not in user

    session_cookie = api.client.cookies.get(settings.session_cookie_name)
    assert session_cookie, "a session cookie must be set on register"

    body = api.get("/api/v1/auth/session").json()
    assert body["csrf_token"]
    assert body["user"]["email"] == "alice@example.com"
    # The credential is never exposed in a JSON body for JS to persist.
    assert "access_token" not in body and "token" not in body


def test_session_cookie_is_httponly(client):
    password = _new_password()
    response = client.post(
        "/api/v1/auth/register",
        json={"email": "cookie-check@example.com", "password": password},
    )
    header = " ; ".join(response.headers.get_list("set-cookie"))
    assert settings.session_cookie_name in header
    assert "HttpOnly" in header

    # The CSRF cookie is intentionally readable by JS (double-submit).
    csrf_portion = [part for part in response.headers.get_list("set-cookie") if settings.csrf_cookie_name in part]
    assert csrf_portion, "the CSRF cookie must be set alongside the session cookie"
    assert "HttpOnly" not in csrf_portion[0]


def test_duplicate_email_is_rejected(api):
    password = _new_password()
    api.register(email="dup@example.com", password=password)
    response = api.post(
        "/api/v1/auth/register",
        json={"email": "dup@example.com", "password": password},
    )
    assert response.status_code in (400, 409)
    assert response.json()["code"]


def test_short_password_is_rejected(api):
    response = api.post(
        "/api/v1/auth/register",
        json={"email": "shorty@example.com", "password": "abc"},
    )
    assert response.status_code == 422


def test_login_and_logout_lifecycle(api):
    api.register(email="login-flow@example.com")
    password = api.password
    assert password is not None
    api.post("/api/v1/auth/logout")

    assert api.get("/api/v1/auth/session").status_code == 401

    bad = api.post("/api/v1/auth/login", json={"email": "login-flow@example.com", "password": "wrong-password"})
    assert bad.status_code in (400, 401)
    assert bad.json()["code"]

    ok = api.post("/api/v1/auth/login", json={"email": "login-flow@example.com", "password": password})
    assert ok.status_code == 200
    assert api.get("/api/v1/auth/session").status_code == 200


def test_state_changing_request_without_csrf_header_is_rejected(client):
    client.post(
        "/api/v1/auth/register",
        json={"email": "csrf@example.com", "password": _new_password()},
    )
    # Deliberately drop the header while keeping the session cookie.
    response = client.patch("/api/v1/settings", json={"name": "Nope"})
    assert response.status_code == 403
    assert response.json()["code"] == "csrf_failed"


def test_wrong_csrf_token_is_rejected(auth_api):
    response = auth_api.patch(
        "/api/v1/settings",
        json={"name": "Nope"},
        headers={"X-CSRF-Token": "not-the-real-token"},
    )
    assert response.status_code == 403


def test_unauthenticated_request_is_401_not_500(client):
    response = client.get("/api/v1/projects")
    assert response.status_code == 401
    body = response.json()
    assert set(body) >= {"code", "message", "request_id"}
    assert "Traceback" not in response.text


def test_password_change_requires_the_current_password(auth_api):
    current_password = auth_api.password
    assert current_password is not None
    new_password = _new_password()
    wrong = auth_api.post(
        "/api/v1/auth/password/change",
        json={"current_password": "nope", "new_password": new_password},
    )
    # A wrong current password is reported as a field validation error.
    assert wrong.status_code in (400, 401, 403, 422)

    right = auth_api.post(
        "/api/v1/auth/password/change",
        json={"current_password": current_password, "new_password": new_password},
    )
    assert right.status_code == 200

    email = auth_api.user["email"]
    auth_api.post("/api/v1/auth/logout")
    assert auth_api.post("/api/v1/auth/login", json={"email": email, "password": new_password}).status_code == 200
    assert auth_api.post("/api/v1/auth/login", json={"email": email, "password": current_password}).status_code in (400, 401)


def test_error_bodies_never_leak_internals(api):
    response = api.post("/api/v1/auth/login", json={"email": "ghost@example.com", "password": "whatever123"})
    assert response.status_code in (400, 401)
    text = response.text.lower()
    for leak in ("traceback", "sqlalchemy", "site-packages", "password_hash", "bcrypt:"):
        assert leak not in text
