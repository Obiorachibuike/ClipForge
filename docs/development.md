# Development

## Requirements

- Python 3.11 or 3.12
- Node 20+
- FFmpeg — optional for API-only work; processing handlers use it. The worker
  requirements include `imageio-ffmpeg` as a fallback when no system binary is
  installed.
- PostgreSQL and Redis — optional for local work. Development can use SQLite
  and an in-process queue; production requires shared PostgreSQL, Redis and
  object storage.

`apps/api/requirements.txt` is the lightweight HTTP/API runtime used by Vercel.
The Docker image installs `apps/api/requirements-worker.txt` (which includes the
API set plus media/ingestion tools); local development and tests should install
`apps/api/requirements-dev.txt`.

## First run

```bash
# 1. Python environment
python3 -m venv .venv
./.venv/bin/python -m pip install -r apps/api/requirements-dev.txt

# 2. Environment
cp .env.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(48))"   # -> SECRET_KEY
python3 -c "import secrets; print(secrets.token_urlsafe(48))"   # -> JWT_SECRET

# 3. API (creates tables on first start; runs an inline worker)
./.venv/bin/python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000 \
    --app-dir apps/api

# 4. Web app (separate terminal)
cd apps/web && npm install && npm run dev
```

Open <http://localhost:5173>. Vite proxies `/api`, `/ws` and `/healthz` to the
API, so the browser stays on a single origin and the session cookie works.

Interactive API docs are at <http://localhost:8000/docs>.

## Demo media

There is no sample video in the repository: it is generated, which keeps the
tree small and makes the pipeline reproducible.

```bash
python scripts/make_demo_asset.py --out demo
```

That writes `demo/creator_masterclass.mp4` (143 s, 1920×1080, loudness-normalized
to −16 LUFS), `demo/narration.txt`, and `demo/manifest.json` describing the
ground truth (segment boundaries, and which section is "the good part"). Use
`--no-video` for audio only.

Seeding an account plus a processed video in one command:

```bash
python scripts/seed.py --user --asset demo/creator_masterclass.mp4 \
    --narration-script demo/narration.txt --process
```

## Tests

```bash
# Backend
./.venv/bin/python -m pytest apps/api/tests -q

# Frontend
cd apps/web && npm test

# Types and API drift
cd apps/web && npx tsc --noEmit
cd apps/web && npm run build            # tsc -b + vite build
cd apps/web && npm run check:api        # compares types to the live OpenAPI schema
```

`apps/api/tests/test_real_media_pipeline.py` is marked `integration` and builds a
real H.264/AAC video from espeak-ng narration, uploads it through the HTTP API,
runs the worker handlers and asserts the rendered 1080x1920 MP4 with burned-in
captions. It skips with an explicit reason when FFmpeg or espeak-ng is missing:

```bash
./.venv/bin/python -m pytest apps/api/tests/test_real_media_pipeline.py -q -m integration
```

`scripts/check_api_types.py` imports the FastAPI app, parses
`apps/web/src/types/api.ts`, and fails if a field is added, renamed or removed on
either side. It re-execs into `.venv` automatically if FastAPI is not importable.

### Database migrations

```bash
# Apply the baseline to a fresh database (DATABASE_URL or -x url=...)
./.venv/bin/alembic upgrade head

# Databases created earlier by AUTO_CREATE_TABLES already match the baseline
./.venv/bin/alembic stamp 0001_initial_schema   # then set AUTO_CREATE_TABLES=false

# Model/migration drift check (needs a disposable database)
./.venv/bin/alembic check
```

`apps/api/tests/test_migrations.py` runs the real commands against a temporary
SQLite database and compares every table, column, index and foreign-key action
with `Base.metadata`. Add a new revision for each model change; never edit an
applied migration.

### End-to-end (Playwright)

```bash
python scripts/make_demo_asset.py --out demo     # the spec uploads this file
cd apps/web && npx playwright install chromium
cd apps/web && npm run test:e2e
```

`playwright.config.ts` starts both servers itself (API on 8000, Vite on 5173)
against a scratch database, or reuses an existing pair when
`PLAYWRIGHT_REUSE_SERVERS=1`.

> **Not yet executed in the development sandbox.** The environment that produced
> this code has no way to download a browser binary, so the specs have never been
> run. They are written against the real UI and real API; see
> `apps/web/e2e/README.md` for exactly what is and is not verified.

## Layout

```
apps/api/app/
  api/v1/        HTTP routers, one module per resource
  core/          config, db, security, errors, rate limiting, redis
  models/        SQLAlchemy models
  schemas/       Pydantic request/response models
  services/
    ai/          provider interfaces + implementations
    billing/     plans, providers, subscriptions, usage
    media/       ffmpeg, ass (captions), vision, framing, render spec
    pipeline/    job handlers: video_jobs, transcribe, discover, framing, render
    storage/     local + S3 backends behind one interface
  websocket/     socket route, connection manager, tickets
  workers/       worker process, job handlers, inline worker
apps/web/src/
  components/    layout, ui primitives, guards, toasts
  hooks/         useProjects, useUpload
  lib/           api client, query client, live provider, formatting
  pages/         one module per route
  stores/        zustand: auth, ui (toasts), events
  ws/            LiveClient (reconnect, heartbeat, resync)
apps/processor/rust/   optional Rust sidecar
infrastructure/        Dockerfile for the API/worker image
scripts/               demo asset, seeding, API type check
```

## Conventions

- **No fake functionality.** If something is not implemented, it is absent or
  disabled in the UI — never a button that pretends to work and never a mocked
  result. Capability reporting (`GET /capabilities`) tells the truth about what
  the deployment can actually do.
- **Jobs, not requests.** Any operation longer than a moment becomes a job.
- **Media never in the database, video never in the client's memory.**
- **Failures are actionable.** Users see a sentence; the stack trace goes to the
  log with a `request_id` they can quote.
- Tests assert behaviour that was actually observed. When a test disagrees with
  the code, work out which is wrong before changing either.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `Media processing is disabled` | FFmpeg not found. Install it, set `MEDIA_FFMPEG_PATH`, or install `imageio-ffmpeg`. |
| Jobs stay `queued` | No worker running. Set `WORKER_INLINE=true` or start `python -m app.workers.worker`. |
| `queue.redis_required_unavailable` in production | Configure reachable Redis. In-process queues are for development/tests only and are never used as the production job queue. |
| Uploads fail at `init` | Check the extension/MIME allowlists and `MEDIA_MAX_UPLOAD_BYTES`. |
| Truncated/no transcript | No ASR weights and no configured API provider. Paste a script so alignment can produce real timings, or configure a provider. |
| `403 csrf_failed` on a write | The `X-CSRF-Token` header is missing or stale. Refresh the page; the client re-reads the cookie. |
| WebSocket reconnects forever | Check `/ws/v1/events` is proxied with upgrade headers, and that `POST /api/v1/ws-ticket` succeeds. |
