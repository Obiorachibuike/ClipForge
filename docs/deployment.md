# Deployment

## Vercel Services (Beta)

The root `vercel.json` deploys one Vercel project as three independently built
services:

| Service | Root | Exposure | Notes |
| --- | --- | --- | --- |
| `web` | `apps/web` | Public catch-all `/(.*)` | Vite SPA |
| `api` | `apps/api` | Public `/api/*`, `/ws/*`, health and API docs | FastAPI (`app.main:app`) |
| `processor` | `apps/processor/rust` | Internal only | Rust/FFmpeg container |

The `api` service declares a service binding to `processor`. Vercel injects its
internal base URL as `PROCESSOR_URL`; **do not create that variable manually**.
Set `PROCESSOR_ENABLED=true` if you want the optional sidecar used. The Python
pipeline continues locally when the sidecar is disabled or unavailable.

Run the multi-service topology locally with:

```bash
vercel dev -L
```

For a real deployment, configure at least `SECRET_KEY`, `JWT_SECRET`,
`ENCRYPTION_KEY`, `DATABASE_URL`, `STORAGE_BACKEND=s3`, and the relevant `S3_*`
values in the Vercel project. Vercel filesystems are ephemeral, so SQLite and
local media storage are suitable only for a local smoke test. Set
`ENVIRONMENT=production`, `COOKIE_SECURE=true`, `FRONTEND_URL` and
`CORS_ORIGINS` to the deployment's HTTPS origin. URL imports also need outbound
access to the source platform; private/authenticated videos are intentionally
not accepted.

> The Rust processor currently receives local workspace paths from the Python
> pipeline. Separate Vercel services do not share a filesystem, so media calls
> safely fall back to Python until the processor contract is changed to use
> signed object-storage URLs. Its health endpoint and binding still work, and it
> remains private because no top-level rewrite targets it.

## Docker Compose

```bash
cp .env.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(48))"   # SECRET_KEY
python3 -c "import secrets; print(secrets.token_urlsafe(48))"   # JWT_SECRET

docker compose up --build
```

Open <http://localhost:8080>. That single command starts all eight services:

| Service | Port | Notes |
| --- | --- | --- |
| `web` | `${WEB_PORT:-8080}` → 80 | nginx serving the built SPA and proxying `/api`, `/ws` |
| `api` | `${API_PORT:-8000}` → 8000 | FastAPI; runs an inline worker pool by default |
| `worker` | – | Dedicated job runner, scaled with `--scale worker=N` |
| `postgres` | internal | Metadata only |
| `redis` | internal | Queue, pub/sub, rate limits |
| `minio` | `${MINIO_PORT:-9000}`, console `9001` | S3-compatible object storage |
| `minio-init` | – | Creates the bucket once, then exits |
| `processor` | internal | Optional Rust sidecar |

`docker compose ps` shows health; `docker compose logs -f worker` follows job
output. Wait for `minio-init` to finish before the first upload.

### First upload

1. Register at <http://localhost:8080/register>.
2. Create a project, upload a video. It streams in chunks and never buffers the
   whole file in memory.
3. Watch the job progress: the WebSocket drives it live, REST recovers it after a
   refresh.

Without ASR weights and without an API key, transcription will report what is
missing instead of inventing a transcript. Either paste a narration script on the
project page (real DTW alignment produces word timings) or set `OPENAI_API_KEY`.

## Configuration

Everything is environment-driven; `.env.example` documents every variable with
its default. The ones that matter most in production:

| Variable | Why |
| --- | --- |
| `SECRET_KEY`, `JWT_SECRET` | Required. Compose refuses to start without `SECRET_KEY`. |
| `ENCRYPTION_KEY` | Fernet key for provider API keys at rest. Derive from `SECRET_KEY` if unset. |
| `COOKIE_SECURE=true` | Required behind HTTPS, or the session cookie leaks over HTTP. |
| `CORS_ORIGINS` | Must include the public origin. |
| `STORAGE_BACKEND`, `S3_*` | `s3` in compose (MinIO/R2/S3). `auto` probes before adopting S3. |
| `DATABASE_URL` | PostgreSQL in compose; SQLite works for a single-box trial. |
| `WORKER_CONCURRENCY`, `RENDER_CONCURRENCY` | Throughput vs. CPU. Rendering is the expensive one. |
| `MEDIA_MAX_UPLOAD_BYTES` | Also reflected in plan limits. |
| `PROCESSOR_ENABLED` | Enable the Rust sidecar; the app works without it. |

## Scaling

- **API tier:** `docker compose up -d --scale api=3`. Sessions are cookie-based
  and the queue lives in Redis, so any replica can serve any request.
- **Job throughput:** `docker compose up -d --scale worker=8`. Workers are
  stateless and pull from the same queue.
- **Rendering is CPU-bound.** Cap `RENDER_CONCURRENCY` per worker to the cores
  you can spare, then add workers rather than raising it.
- **More than one API replica:** set `API_WORKER_INLINE=false` so the HTTP tier
  does not compete with encoding for CPU.
- The Rust sidecar scales the same way; it holds no state.

## Object storage

`minio-init` creates one bucket. For Cloudflare R2 or S3:

```env
STORAGE_BACKEND=s3
S3_ENDPOINT=https://<account>.r2.cloudflarestorage.com
S3_ACCESS_KEY=...
S3_SECRET_KEY=...
S3_BUCKET=clipforge-media
S3_REGION=auto
S3_PATH_STYLE=false
```

Video binaries live only in object storage. The database stores keys, sizes and
metadata — never file contents.

## Database migrations

`AUTO_CREATE_TABLES=true` creates tables on startup, which is convenient for a
single box and is what compose does by default. For a real deployment:

1. Generate a baseline: `alembic revision --autogenerate -m "initial"` with
   `DATABASE_URL` pointed at the target database (Alembic is already a
   dependency).
2. Set `AUTO_CREATE_TABLES=false`.
3. Run `alembic upgrade head` as a deploy step before starting new containers.

There is no `alembic/` directory in the tree yet — the migrations environment has
not been initialised, so step 1 is a required first action. Until then keep
`AUTO_CREATE_TABLES=true` and treat schema changes as requiring a maintenance
window.

## Health and observability

| Endpoint | Purpose |
| --- | --- |
| `GET /healthz` | Liveness. Cheap; no dependencies touched. |
| `GET /readyz` | Readiness: database, storage, queue, FFmpeg, and sidecar reachability. Returns 503 when FFmpeg is missing, because no media job could succeed. |
| `GET /api/v1/system/status` | Capabilities, job counts, queue depth, worker stats, WebSocket connections. |
| `GET /api/v1/system/metrics` | Counters for scraping. |
| `GET /docs`, `/openapi.json` | Interactive API documentation and the raw schema. |

Logs are structured (`LOG_JSON=true`), and every response carries an
`X-Request-ID` that also appears on error bodies — worth capturing in support
reports.

The Rust sidecar exposes `/healthz` and `/readyz` of its own; both are wired to
container healthchecks.

## Production checklist

- [ ] `SECRET_KEY`, `JWT_SECRET`, `ENCRYPTION_KEY` are random and stored as
      secrets, not in the image or the repo.
- [ ] `ENVIRONMENT=production`, `DEBUG=false`, `LOG_JSON=true`.
- [ ] `COOKIE_SECURE=true` and TLS terminated in front of `web`.
- [ ] `CORS_ORIGINS` lists exactly the public origin(s).
- [ ] PostgreSQL and Redis are managed services with backups; **Redis has
      `appendonly yes`** because it holds the job queue.
- [ ] Object storage has a lifecycle/retention policy for abandoned uploads.
- [ ] `AUTO_CREATE_TABLES=false` once migrations are in place.
- [ ] `RATE_LIMIT_ENABLED=true` and limits tuned to real traffic.
- [ ] Provider keys set only if you want hosted AI; otherwise plan for local
      weights (below).
- [ ] A worker health alert: a queue that grows while workers are up means a
      crash-looping handler.
- [ ] Upload size limits match your storage budget and plan tiers.

## Known deployment gaps

Stated plainly, because a checklist that overstates readiness is worse than none:

- **The Rust processor has never been compiled.** No Rust toolchain is available
  in the development sandbox and the toolchain download hosts are blocked.
  `docker compose build processor` is its first real compile; expect to fix
  diagnostics there. Nothing else depends on it — leave
  `PROCESSOR_ENABLED=false` for a first deployment.
- **The Compose stack has not been started end to end** in that same sandbox (no
  Docker). The configuration is statically verified instead: `apps/api/tests/test_deployment.py`
  checks service definitions, that every compose env var is actually read by
  `Settings`, that `COPY` sources exist in their build contexts, that the nginx
  upstream matches the compose service name, and that the SPA fallback and
  WebSocket upgrade headers are present.
- **`minio-init`** relies on the `mc` image alias syntax; if your MinIO version
  differs, create the bucket manually.
- **No CI pipeline** is committed yet. The intended jobs are
  `pytest`, `vitest`, `tsc`, `check:api`, and the Playwright suite.

## Local ASR without a hosted provider

`faster-whisper` needs model weights from Hugging Face. In a network-restricted
environment, either:

- pre-download weights and set `WHISPER_DOWNLOAD_ROOT` with
  `WHISPER_LOCAL_FILES_ONLY=true`, or
- paste a narration script and let DTW alignment produce real word timings
  (this is what the demo asset uses), or
- point `AI_TRANSCRIPTION_PROVIDER` at an OpenAI-compatible endpoint you host.

The system reports which of these is in use — `GET /capabilities` never claims a
provider is available when it is not.
