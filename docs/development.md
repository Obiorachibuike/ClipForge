# Development

## Requirements

- Python 3.11 or 3.12
- Node 20+
- FFmpeg — optional, but media features need it. `imageio-ffmpeg` ships a static
  build, so the common paths work without a system install.
- PostgreSQL and Redis — optional for local work. The API falls back to SQLite
  and an in-process queue automatically, and says so in the startup log.

## First run

```bash
# 1. Python environment
python3 -m venv .venv
./.venv/bin/python -m pip install -r apps/api/requirements.txt

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
# Backend — 95 tests
./.venv/bin/python -m pytest apps/api/tests -q

# Frontend — 99 tests
cd apps/web && npm test

# Types and API drift
cd apps/web && npx tsc --noEmit
cd apps/web && npm run check:api        # compares types to the live OpenAPI schema
```

`scripts/check_api_types.py` imports the FastAPI app, parses
`apps/web/src/types/api.ts`, and fails if a field is added, renamed or removed on
either side. It re-execs into `.venv` automatically if FastAPI is not importable.

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
| `redis.unavailable` in the log | Expected without Redis: the queue and cache fall back to in-process. Fine for one process, not for multiple workers. |
| Uploads fail at `init` | Check the extension/MIME allowlists and `MEDIA_MAX_UPLOAD_BYTES`. |
| Truncated/no transcript | No ASR weights and no configured API provider. Paste a script so alignment can produce real timings, or configure a provider. |
| `403 csrf_failed` on a write | The `X-CSRF-Token` header is missing or stale. Refresh the page; the client re-reads the cookie. |
| WebSocket reconnects forever | Check `/ws/v1/events` is proxied with upgrade headers, and that `POST /api/v1/ws-ticket` succeeds. |
