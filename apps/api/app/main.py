"""FastAPI application entry point.

Run locally:
    uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import text

from app.api.v1 import account, auth, clips, jobs, projects, renders, transcripts, videos
from app.core.config import settings
from app.core.db import get_engine, get_session_factory, init_engine_if_needed
from app.core.errors import AppError
from app.core.logging import configure_logging, get_logger
from app.core.rate_limit import check_rate_limit
from app.services import events as ev
from app.services.bootstrap import bootstrap

log = get_logger(__name__)

DESCRIPTION = """
**ClipForge** turns long videos into short-form vertical clips.

The API is versioned under `/api/v1`:

* **Authentication** - cookie sessions with CSRF double-submit tokens
* **Projects** - containers for one source video and its clips
* **Videos & Uploads** - chunked, resumable uploads and range-enabled streaming
* **Transcripts** - word-level timings, speakers, searching, safe text edits
* **Clips** - AI-discovered suggestions, review, editing, smart crop
* **Captions** - word-level animated caption styles and text edits
* **Headlines** - generated suggestions (LLM or extractive) with explicit selection
* **Renders & Exports** - real FFmpeg renders with live progress and downloads
* **Jobs** - every long operation is a job; state is queryable and cancellable
* **Realtime** - `/ws/v1/events` streams job progress over WebSocket

Interactive documentation is available at `/docs` and the raw schema at
`/openapi.json`.
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    log.info("app.starting", environment=settings.environment, prefix=settings.api_prefix)
    init_engine_if_needed()
    capabilities = bootstrap(seed=True)
    app.state.capabilities = capabilities

    stop_inline = None
    if settings.worker_inline:
        from app.workers.inline import start_inline_worker, stop_inline_worker

        start_inline_worker()
        stop_inline = stop_inline_worker
    else:
        log.info("app.worker_inline_disabled", detail="Run workers separately: python -m app.workers.worker")

    try:
        yield
    finally:
        if stop_inline:
            stop_inline()
        ev.get_event_bus().shutdown()
        log.info("app.stopped")


app = FastAPI(
    title="ClipForge API",
    description=DESCRIPTION,
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
    openapi_tags=[
        {"name": "Authentication", "description": "Sessions, registration, password flows."},
        {"name": "Projects", "description": "Projects group a source video, its clips and renders."},
        {"name": "Videos", "description": "Uploads, streaming and processing triggers."},
        {"name": "Transcripts", "description": "Word-level transcripts and text edits."},
        {"name": "Clips", "description": "Suggestions, clips, captions, headlines, crop."},
        {"name": "Renders & Exports", "description": "Rendering, progress, downloads and history."},
        {"name": "Jobs", "description": "Job history, cancellation and retries."},
        {"name": "Account", "description": "Settings, AI providers, usage and billing."},
        {"name": "System", "description": "Health, capabilities and realtime connectivity."},
    ],
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_origin_regex=r"https://.*\.e2b\.app",
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID", "Content-Range", "Accept-Ranges"],
)


@app.middleware("http")
async def observability_middleware(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    request.state.request_id = request_id
    started = time.time()
    try:
        response = await call_next(request)
    except AppError:
        raise
    except Exception:
        log.exception("request.unhandled", path=request.url.path, method=request.method, request_id=request_id)
        return JSONResponse(
            status_code=500,
            content={
                "code": "internal_error",
                "message": "Something went wrong on our side. Please try again.",
                "details": {},
                "request_id": request_id,
            },
            headers={"X-Request-ID": request_id},
        )
    duration_ms = (time.time() - started) * 1000
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Response-Time"] = f"{duration_ms:.0f}ms"
    # Baseline security headers for a JSON API. The web app adds CSP for pages.
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    if request.url.path not in ("/healthz", "/readyz", "/metrics"):
        log.info(
            "request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            ms=round(duration_ms, 1),
            request_id=request_id,
        )
    return response


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    """Global limiter for API traffic; specific routes apply stricter buckets."""
    if settings.rate_limit_enabled and request.url.path.startswith(settings.api_prefix):
        identity = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (
            request.client.host if request.client else "unknown"
        )
        try:
            check_rate_limit(identity, "global", settings.rate_limit_requests, settings.rate_limit_window_seconds)
        except AppError as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content={"code": exc.code, "message": exc.message, "details": exc.details, "request_id": ""},
                headers={"Retry-After": str(exc.details.get("retry_after", 60))},
            )
    return await call_next(request)


# ------------------------------------------------------------- error handling ---
@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    headers = {}
    if exc.code == "rate_limited":
        headers["Retry-After"] = str(exc.details.get("retry_after", 60))
    if exc.code == "session_expired":
        headers["X-Session-Expired"] = "1"
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "code": exc.code,
            "message": exc.message,
            "details": exc.details,
            "request_id": getattr(request.state, "request_id", ""),
        },
        headers=headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Turn pydantic validation errors into messages a user can act on."""
    details = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ()) if part not in ("body", "query", "path"))
        details.append({"field": location, "message": error.get("msg", "Invalid value")})
    first = details[0] if details else {"field": "", "message": "Invalid request"}
    message = f"{first['field']}: {first['message']}" if first["field"] else first["message"]
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "code": "validation_error",
            "message": message,
            "details": {"errors": details},
            "request_id": getattr(request.state, "request_id", ""),
        },
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    log.exception("app.unhandled", path=request.url.path, error=str(exc)[:500])
    return JSONResponse(
        status_code=500,
        content={
            "code": "internal_error",
            "message": "Something went wrong on our side. Please try again.",
            "details": {},
            "request_id": getattr(request.state, "request_id", ""),
        },
    )


# --------------------------------------------------------------------- routes ---
API = settings.api_prefix
app.include_router(auth.router, prefix=API)
app.include_router(account.router, prefix=API)
app.include_router(projects.router, prefix=API)
app.include_router(videos.router, prefix=API)
app.include_router(transcripts.router, prefix=API)
app.include_router(clips.router, prefix=API)
app.include_router(renders.router, prefix=API)
app.include_router(jobs.router, prefix=API)

from app.api.deps import CurrentUser  # noqa: E402
from app.websocket.router import create_ticket, events_socket  # noqa: E402


def _ws_ticket(user: CurrentUser) -> dict:
    return create_ticket(user)


app.add_api_route(
    f"{API}/ws-ticket",
    _ws_ticket,
    methods=["POST"],
    tags=["System"],
    response_model=dict,
    summary="Mint a short-lived WebSocket ticket",
)
app.add_api_websocket_route("/ws/v1/events", events_socket)


@app.get("/healthz", tags=["System"], response_class=PlainTextResponse)
def healthz() -> str:
    """Liveness probe: process is up."""
    return "ok"


@app.get("/readyz", tags=["System"])
def readyz() -> JSONResponse:
    """Readiness probe: database, storage and queue are usable."""
    checks: dict[str, object] = {}
    healthy = True
    try:
        db = get_session_factory()()
        try:
            db.execute(text("SELECT 1"))
            checks["database"] = "ok"
        finally:
            db.close()
    except Exception as exc:
        healthy = False
        checks["database"] = f"error: {type(exc).__name__}"

    from app.services.queue import get_queue
    from app.services.storage import get_storage

    try:
        checks["storage"] = get_storage().name
    except Exception as exc:
        healthy = False
        checks["storage"] = f"error: {type(exc).__name__}"
    try:
        checks["queue"] = get_queue().backend_name()
    except Exception as exc:
        healthy = False
        checks["queue"] = f"error: {type(exc).__name__}"

    from app.services.media.ffmpeg import ffmpeg_bin

    try:
        checks["ffmpeg"] = bool(ffmpeg_bin())
        if not checks["ffmpeg"]:
            healthy = False
    except Exception:
        checks["ffmpeg"] = False
        healthy = False

    return JSONResponse(status_code=200 if healthy else 503, content={"status": "ok" if healthy else "degraded", "checks": checks})


@app.get(f"{API}/system/status", tags=["System"])
def system_status() -> dict:
    from app.services.job_service import JobService
    from app.services.queue import get_queue
    from app.workers.inline import inline_worker_stats

    db = get_session_factory()()
    try:
        stats = JobService.stats(db)
    finally:
        db.close()
    from app.websocket.manager import manager

    return {
        "capabilities": getattr(app.state, "capabilities", {}),
        "jobs": stats,
        "queue": {"backend": get_queue().backend_name(), "depth": get_queue().depth()},
        "worker": inline_worker_stats() or {"mode": "external"},
        "websockets": manager.stats(),
        "environment": settings.environment,
    }


@app.get(f"{API}/system/metrics", tags=["System"], response_class=PlainTextResponse)
def metrics() -> str:
    """Prometheus-style metrics (text exposition format)."""
    from app.services.job_service import JobService
    from app.services.queue import get_queue
    from app.websocket.manager import manager

    db = get_session_factory()()
    try:
        stats = JobService.stats(db)
    finally:
        db.close()
    lines = [
        "# HELP clipforge_jobs_total Jobs by status",
        "# TYPE clipforge_jobs_total gauge",
    ]
    for status_name, count in sorted(stats["by_status"].items()):
        lines.append(f'clipforge_jobs_total{{status="{status_name}"}} {count}')
    lines += [
        "# HELP clipforge_queue_depth Queued jobs awaiting a worker",
        "# TYPE clipforge_queue_depth gauge",
        f"clipforge_queue_depth {get_queue().depth()}",
        "# HELP clipforge_active_jobs Jobs currently executing",
        "# TYPE clipforge_active_jobs gauge",
        f"clipforge_active_jobs {len(get_queue().active_jobs())}",
        "# HELP clipforge_websocket_connections Live WebSocket connections",
        "# TYPE clipforge_websocket_connections gauge",
        f"clipforge_websocket_connections {manager.stats()['connections']}",
    ]
    return "\n".join(lines) + "\n"


@app.get("/", include_in_schema=False)
def root() -> dict:
    return {
        "name": "ClipForge API",
        "version": app.version,
        "docs": "/docs",
        "api": API,
        "realtime": "/ws/v1/events",
    }
