"""Shared pytest fixtures.

The API is imported lazily *after* the environment is configured, because
`Settings` is read at import time and the database engine is a module-level
singleton. Each test session therefore gets its own SQLite file and storage
root, and nothing touches the developer's `.data/` directory.
"""
from __future__ import annotations

import os
import secrets
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Iterator

import pytest

def _find_repo_root() -> Path:
    """Walk up until the `apps/api` layout is found (robust to pytest rootdir)."""
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "apps" / "api" / "app").is_dir():
            return candidate
    raise RuntimeError("could not locate the repository root from the test file")


REPO_ROOT = _find_repo_root()
API_ROOT = REPO_ROOT / "apps" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="clipforge-tests-"))


def _configure_environment() -> None:
    """Set every knob that must be in place before `app.core.config` loads."""
    os.environ.update(
        {
            "ENVIRONMENT": "test",
            "DEBUG": "false",
            "LOG_LEVEL": "WARNING",
            "DATABASE_URL": f"sqlite:///{_TMP / 'test.db'}",
            "AUTO_CREATE_TABLES": "true",
            "SECRET_KEY": "test-secret-key-not-for-production-use",
            "JWT_SECRET": "test-jwt-secret-not-for-production-use",
            "COOKIE_SECURE": "false",
            "STORAGE_BACKEND": "local",
            "STORAGE_LOCAL_ROOT": str(_TMP / "storage"),
            "QUEUE_BACKEND": "memory",
            "WORKER_INLINE": "false",  # tests drive jobs explicitly
            "RATE_LIMIT_ENABLED": "false",
            "SEED_DEMO_DATA": "false",
            "VISION_PROVIDER": "none",
            "PROCESSOR_ENABLED": "false",
        }
    )


_configure_environment()


@pytest.fixture(scope="session")
def app_module():
    from app.main import app

    return app


@pytest.fixture(scope="session", autouse=True)
def _prepare_database(app_module):
    """Create the schema once for the whole session."""
    from app.core.db import dispose_engine, get_engine, init_engine_if_needed

    init_engine_if_needed()
    yield
    dispose_engine()
    shutil.rmtree(_TMP, ignore_errors=True)


@pytest.fixture()
def db():
    """A session bound to the test database."""
    from app.core.db import get_session_factory

    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client(app_module) -> Iterator["TestClient"]:
    """Unauthenticated HTTP client with the app lifespan running."""
    from fastapi.testclient import TestClient

    with TestClient(app_module, base_url="http://testserver") as test_client:
        yield test_client


def _new_test_password() -> str:
    """Generate a valid credential per test without checking one into Git."""
    return f"Test{secrets.token_hex(24)}7"


def _unique_email(prefix: str = "user") -> str:
    import uuid

    return f"{prefix}-{uuid.uuid4().hex[:12]}@example.com"


class ApiClient:
    """TestClient wrapper that keeps the CSRF double-submit token in sync.

    The real browser reads `clipforge_csrf` (a non-HttpOnly cookie by design)
    and echoes it in `X-CSRF-Token`; this mirrors that exactly so the tests
    exercise the production CSRF path instead of bypassing it.
    """

    def __init__(self, client, csrf_token: str = ""):
        self.client = client
        self.csrf_token = csrf_token
        self.user: dict = {}
        # Generated for each test client so no reusable-looking credential is
        # stored in the repository. Tests that need to log in can reuse it.
        self.password: str | None = None

    # -- plumbing ---------------------------------------------------------
    def _refresh_csrf(self) -> None:
        from app.core.config import settings

        token = self.client.cookies.get(settings.csrf_cookie_name)
        if token:
            self.csrf_token = token

    def _headers(self, extra: dict | None = None) -> dict:
        from app.core.config import settings

        headers = {"X-CSRF-Token": self.csrf_token or self.client.cookies.get(settings.csrf_cookie_name, "")}
        if extra:
            headers.update(extra)
        return headers

    def request(self, method: str, url: str, **kwargs):
        if method.upper() not in {"GET", "HEAD", "OPTIONS"}:
            kwargs.setdefault("headers", {})
            kwargs["headers"] = {**self._headers(), **kwargs["headers"]}
        response = self.client.request(method, url, **kwargs)
        self._refresh_csrf()
        return response

    def get(self, url: str, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs):
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs):
        return self.request("PUT", url, **kwargs)

    def patch(self, url: str, **kwargs):
        return self.request("PATCH", url, **kwargs)

    def delete(self, url: str, **kwargs):
        return self.request("DELETE", url, **kwargs)

    # -- convenience ------------------------------------------------------
    def register(self, *, email: str | None = None, password: str | None = None, name: str = "Test User"):
        password = _new_test_password() if password is None else password
        self.password = password
        payload = {"email": email or _unique_email(), "password": password, "name": name}
        response = self.post("/api/v1/auth/register", json=payload)
        assert response.status_code in (200, 201), response.text
        self.user = response.json()["user"]
        self._refresh_csrf()
        return self.user

    def login(self, email: str, password: str | None = None):
        password = self.password if password is None else password
        if password is None:
            raise ValueError("register a test user or pass its password before logging in")
        response = self.post("/api/v1/auth/login", json={"email": email, "password": password})
        assert response.status_code == 200, response.text
        self.user = response.json()["user"]
        self._refresh_csrf()
        return self.user


@pytest.fixture()
def api(client) -> ApiClient:
    """Anonymous API client."""
    return ApiClient(client)


@pytest.fixture()
def auth_api(client) -> ApiClient:
    """Registered, signed-in API client."""
    wrapper = ApiClient(client)
    wrapper.register()
    return wrapper


@pytest.fixture()
def ticket_api(app_module) -> Iterator[ApiClient]:
    """A client with *no* session cookie, so only a `?ticket=` can authenticate it."""
    from fastapi.testclient import TestClient

    with TestClient(app_module, base_url="http://testserver") as test_client:
        yield ApiClient(test_client)


@pytest.fixture()
def second_api(app_module) -> Iterator[ApiClient]:
    """A second, independent user — used for authorization tests."""
    from fastapi.testclient import TestClient

    with TestClient(app_module, base_url="http://testserver") as test_client:
        wrapper = ApiClient(test_client)
        wrapper.register()
        yield wrapper
