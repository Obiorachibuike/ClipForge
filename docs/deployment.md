# Deployment

## Vercel Services (Beta)

The root `vercel.json` deploys one Vercel project as three independently built
services:

| Service | Root | Exposure | Notes |
| --- | --- | --- | --- |
| `web` | `apps/web` | Public catch-all `/(.*)` | Vite SPA |
| `api` | `apps/api` | Public `/api/*`, `/ws/*`, health and API docs | FastAPI (`app.main:app`); installs only `apps/api/requirements.txt` |
| `processor` | `apps/processor/rust` | Internal only | Optional Rust media sidecar |

The API service binds to the Rust `processor` service and Vercel injects
`PROCESSOR_URL`; do not create that variable manually. This binding does **not**
run background jobs and is not a substitute for the processing worker. Keep
`PROCESSOR_ENABLED=true` only when that optional acceleration is configured for
its caller; Python implementations remain available as fallbacks.

### API and processing worker are separate deployments

Vercel hosts the HTTP/API service only. It validates requests, writes upload
parts and job metadata, enqueues job IDs in Redis, and serves status/progress.
It does not run `VIDEO_PROBE`, transcription, discovery, framing or rendering.
Run the existing Docker-compatible worker image on a separate container runtime
that can reach the **same** PostgreSQL, Redis and S3-compatible bucket as Vercel.
No local filesystem is shared between Vercel and the worker.

The root `infrastructure/Dockerfile.api` is intentionally the shared full API /
worker image. It installs `apps/api/requirements-worker.txt`, which includes the
API requirements plus URL-ingestion and media-processing dependencies. Vercel's
FastAPI service remains rooted at `apps/api` and discovers only
`apps/api/requirements.txt`; do not point it at the worker or development
requirements files. `apps/api/requirements-dev.txt` is for local tests and
includes the full worker set plus test tools.

On Vercel, configure these environment variables in the project (secrets should
be stored in Vercel's environment settings, not in the repository):

- `ENVIRONMENT=production`, `SECRET_KEY`, `JWT_SECRET`, `ENCRYPTION_KEY`
- `DATABASE_URL` for PostgreSQL and `AUTO_CREATE_TABLES` according to the
  migration process below
- `QUEUE_BACKEND=redis`, `REDIS_URL` for the durable shared job queue;
  `CACHE_BACKEND=redis` and the same `REDIS_URL` for shared cache/rate-limit and
  event fan-out behavior
- `STORAGE_BACKEND=s3` and the appropriate `S3_BUCKET`, `S3_REGION`,
  `S3_ENDPOINT` (when not AWS), `S3_ACCESS_KEY`, `S3_SECRET_KEY`, and
  `S3_PATH_STYLE` values
- `WORKER_INLINE=false` and `WORKER_EXTERNAL=true` to keep media execution out
  of Vercel while reporting worker-side media capabilities to the UI
- `COOKIE_SECURE=true`, `FRONTEND_URL`, and `CORS_ORIGINS` for the HTTPS app
  origin
- AI provider credentials/settings needed by your product. Configure the same
  provider environment on the worker so jobs can use the configured providers.

The Docker worker needs matching `DATABASE_URL`, `REDIS_URL`, `QUEUE_BACKEND`,
`STORAGE_BACKEND`, `S3_*`, and AI settings. Its `WORKER_INLINE` must be false.
Build it from the repository root and start the worker command on your container
platform:

```bash
docker build -f infrastructure/Dockerfile.api -t clipforge-worker .
docker run --rm --env-file /path/to/worker.env clipforge-worker python -m app.workers.worker
```

`worker.env` is deployment-specific and must not be committed. Use endpoints
reachable from both Vercel and the container, rather than the Compose-only
`postgres`, `redis`, or `minio` hostnames. A container platform may instead set
the same variables through its secret/environment interface. Scale worker
replicas independently; all replicas pull from Redis and read/write the common
object store.

The API persists each resumable chunk as a separate object beneath the upload
session's parts prefix. `/complete` verifies those objects and queues a probe
job containing the upload ID; it does not concatenate the source in a Vercel
function. The worker assembles the parts into its own temporary workspace,
uploads the finished source object through the same storage abstraction, then
runs the existing probe and media pipeline. The negotiated default chunk is
4 MiB to fit Vercel function request-body limits; clients must use the returned
`chunk_size`. The single-request multipart route is only a small-upload
convenience, not the path for large Vercel uploads.

Vercel filesystems are ephemeral. Do not use SQLite or local media storage for
this topology. URL imports run on the worker and need outbound access to the
source platform; private/authenticated videos are intentionally not accepted.
The Vercel-only processor binding is not automatically available to a worker on
another network. If using the Rust accelerator from the worker, configure a
worker-reachable `PROCESSOR_URL` (or colocate its CLI binary and workspace); if
not, the complete Python media implementation remains active.

Run the Vercel multi-service topology locally with:

```bash
vercel dev -L
```

A successful local preview or static configuration check does not prove that a
Vercel production deployment fits its function bundle limit. Confirm the actual
build output and deployed function size in Vercel after deployment.

## Docker Compose

```bash
cp .env.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(48))"   # SECRET_KEY
python3 -c "import secrets; print(secrets.token_urlsafe(48))"   # JWT_SECRET

docker compose up --build
```

Open <http://localhost:8080>. That command starts the web app, API, worker,
PostgreSQL, Redis, MinIO object storage and the optional Rust processor:

| Service | Port | Notes |
| --- | --- | --- |
| `web` | `${WEB_PORT:-8080}` → 80 | nginx serving the built SPA and proxying `/api`, `/ws` |
| `api` | `${API_PORT:-8000}` → 8000 | FastAPI; uses Redis/object storage and does not process media by default |
| `worker` | – | Dedicated job runner, scaled with `--scale worker=N` |
| `postgres` | internal | Metadata only |
| `redis` | internal | Durable queue, pub/sub and cache |
| `minio` | `${MINIO_PORT:-9000}`, console `9001` | S3-compatible object storage |
| `minio-init` | – | Creates the bucket once, then exits |
| `processor` | internal | Optional Rust sidecar |

`docker compose ps` shows health; `docker compose logs -f worker` follows job
output. Wait for `minio-init` to finish before the first upload. The API does not
mount the worker scratch volume: API-to-worker media transfer uses MinIO/S3.
Compose mounts scratch only into the worker and optional local Rust sidecar so
the sidecar can read that worker's temporary workspace. Compose uses the
dedicated worker by default. For a local single-container experiment, set
`API_WORKER_INLINE=true` and `API_WORKER_EXTERNAL=false`.

### First upload

1. Register at <http://localhost:8080/register>.
2. Create a project and upload a video. The browser sends resumable chunks to
   storage; the worker assembles the full source before probing it.
3. Watch job progress: WebSocket events drive it live, and REST resynchronizes it
after a refresh.

Without ASR weights and without an API key, transcription reports what is
missing instead of inventing a transcript. Either paste a narration script on
the project page (real DTW alignment produces word timings) or configure a
hosted transcription provider.

## Configuration

Everything is environment-driven; `.env.example` documents the variables with
their development defaults. Production requires a durable shared queue and
object store:

| Variable | Why |
| --- | --- |
| `SECRET_KEY`, `JWT_SECRET` | Random application secrets; Compose refuses to start without `SECRET_KEY`. |
| `ENCRYPTION_KEY` | Fernet key for provider API keys at rest. |
| `COOKIE_SECURE=true` | Required behind HTTPS. |
| `CORS_ORIGINS` | Must include the public origin. |
| `DATABASE_URL` | PostgreSQL is required for API/worker coordination. |
| `QUEUE_BACKEND=redis`, `REDIS_URL` | Durable shared job queue; production never falls back to process-local memory. |
| `STORAGE_BACKEND=s3`, `S3_*` | Shared object storage required across API and worker hosts. |
| `WORKER_INLINE`, `WORKER_EXTERNAL` | Decide whether this process runs media jobs or delegates to an external worker. |
| `WORKER_CONCURRENCY`, `RENDER_CONCURRENCY` | Throughput vs. CPU. Rendering is the expensive stage. |
| `MEDIA_MAX_UPLOAD_BYTES` | Maximum source size accepted by the API. |
| `MEDIA_MAX_DURATION_SECONDS` | Source-duration ceiling (default 4 h; `0` disables). Long media is rejected at probe, and URL imports are skipped before download. |
| `UPLOAD_SESSION_TTL_MINUTES` | How long an unfinished resumable upload can still receive chunks; expired sessions return 409 `upload_expired`. |
| `WORKER_RECONCILE_SECONDS` | Startup and periodic stale-job reconciliation interval (default 60 s; `0` disables the periodic pass). |
| `CORS_ORIGIN_REGEX` | Optional exact-pattern allowlist for preview/ephemeral origins. Never use a suffix match. |
| `PROCESSOR_ENABLED`, `PROCESSOR_URL` | Optional Rust acceleration; not the worker queue or media store. |

## Scaling

- **API tier:** scale HTTP replicas independently. Sessions are cookie-based,
  metadata is in PostgreSQL, and Redis/object storage are shared.
- **Job throughput:** scale Docker worker replicas independently; every worker
  pulls the same Redis queue and materializes media from shared object storage.
- **Rendering is CPU-bound.** Cap `RENDER_CONCURRENCY` per worker to the cores
  available, then add worker replicas rather than overcommitting a single host.
- The Rust sidecar is optional and stateless. A remote worker must have a
  reachable processor endpoint or colocated binary for the Rust path; otherwise
  its Python processing implementation is used.

## Object storage

`minio-init` creates one bucket. For Cloudflare R2 or S3:

```env
STORAGE_BACKEND=s3
S3_ENDPOINT=https://<account>.r2.cloudflarestorage.com
S3_ACCESS_KEY=<access-key>
S3_SECRET_KEY=<secret-key>
S3_BUCKET=<bucket-name>
S3_REGION=auto
S3_PATH_STYLE=false
```

Video binaries and temporary upload parts live in object storage. PostgreSQL
stores object keys, sizes, checksums and metadata—not file contents. The worker
cleans the upload-part prefix after it materializes the final source; configure
an object lifecycle rule as a backstop for abandoned uploads.

## Database migrations

The Alembic environment lives in `apps/api/alembic/` and reads `DATABASE_URL`
from the same environment as the API and worker. The initial revision
(`7627644971dd`) creates the complete schema; `alembic check` reports no drift
between the migrations and the ORM models.

```bash
cd apps/api
DATABASE_URL='postgresql+psycopg://user:pass@host:5432/clipforge' alembic upgrade head
DATABASE_URL='...' alembic current      # 7627644971dd (head)
DATABASE_URL='...' alembic check        # no new upgrade operations detected
```

The API/worker image contains `alembic.ini` and the migration directory, so a
deployment can run them from the same image without installing anything:

```bash
docker run --rm --env-file /path/to/worker.env clipforge-worker \
    sh -c 'cd /srv/app/apps/api && alembic upgrade head'
```

### Fresh database

1. Provision PostgreSQL.
2. Run `alembic upgrade head` (above).
3. Set `AUTO_CREATE_TABLES=false` and start the API and workers.

### Existing database created by `AUTO_CREATE_TABLES`

Those tables were created from the same metadata the baseline migration
describes. Stamp the database instead of re-running the DDL:

```bash
DATABASE_URL='...' alembic stamp 7627644971dd
```

Then set `AUTO_CREATE_TABLES=false`. Verify with `alembic check` — it must report
no pending operations. Recovery: take a PostgreSQL snapshot before stamping;
`alembic downgrade base` is destructive and is only for throwaway databases.

`AUTO_CREATE_TABLES=true` remains the development/Compose default. Production
logs a warning when it is still enabled, and the production secret check below
runs at every API and worker start.

Migrations are run from `apps/api` (that is where `alembic.ini` lives). The URL
is read from `DATABASE_URL`; credentials never belong in `alembic.ini`. SQLite
uses batch mode so future `ALTER`s work in development.

`alembic revision --autogenerate -m "..."` output still needs review:
autogenerate can emit `Text()` without the `sa.` prefix for JSONB variants and
can order `CREATE TABLE` statements incorrectly for some constraints. The
baseline was corrected by hand and is checked against the models by
`apps/api/tests/test_migrations.py`.

## Health and observability

| Endpoint | Purpose |
| --- | --- |
| `GET /healthz` | Liveness; no dependencies touched. |
| `GET /readyz` | API readiness: database, storage and queue. It reports local FFmpeg and worker mode but does not require media binaries in the Vercel API process. |
| `GET /api/v1/system/status` | Capabilities, job counts, queue depth, worker mode and WebSocket connections. |
| `GET /api/v1/system/metrics` | Prometheus-style counters. |
| `GET /docs`, `/openapi.json` | Interactive API docs and raw schema. |

A 200 `/readyz` means the API's configured dependencies are available; it does
not prove a separate worker is running or that Vercel's bundle is under its
limit. Monitor worker process health and queue age/depth separately. Logs are
structured (`LOG_JSON=true`), and each HTTP response carries an `X-Request-ID`.

## Fail-closed production configuration

`validate_runtime_settings()` runs at API and worker startup. In
`ENVIRONMENT=production` it **refuses to start** when `SECRET_KEY` or
`JWT_SECRET` is missing, shorter than 32 characters, or still the documented
development placeholder — sessions and provider-key encryption must never be
derived from a public value. It logs (but does not fail on) `COOKIE_SECURE=false`,
`AUTO_CREATE_TABLES=true` and `STORAGE_BACKEND=local`.

Upload chunks are read with a hard byte ceiling derived from the upload session,
so an oversized chunk is rejected with `413` before it is buffered. Source
duration is capped by `MEDIA_MAX_DURATION_SECONDS` after probing (uploads and URL
imports alike), and URL imports also enforce the byte cap during download.

## Production checklist

- [ ] `SECRET_KEY`, `JWT_SECRET`, `ENCRYPTION_KEY` are random secrets, not in the
      image or repository.
- [ ] `ENVIRONMENT=production`, `DEBUG=false`, `LOG_JSON=true`.
- [ ] `COOKIE_SECURE=true`; TLS is terminated in front of the web app.
- [ ] `CORS_ORIGINS` lists exactly the public origin(s).
- [ ] PostgreSQL, Redis and object storage are reachable from both API and worker.
- [ ] `QUEUE_BACKEND=redis`, `WORKER_INLINE=false`, and the Docker worker service
      is running with the same database/Redis/storage environment.
- [ ] `WORKER_EXTERNAL=true` on the Vercel API so capability reporting describes
      the external worker rather than the lightweight function runtime.
- [ ] Redis persistence/availability and a worker queue-age alert are configured.
- [ ] Object storage has lifecycle/retention policy for abandoned upload parts.
- [ ] `alembic upgrade head` ran against the production database (or an existing
      auto-created database was stamped) and `AUTO_CREATE_TABLES=false`.
- [ ] Startup succeeds, proving the placeholder-secret guard passes.
- [ ] `RATE_LIMIT_ENABLED=true` and limits are tuned to real traffic.
- [ ] Provider credentials and upload limits match the product's policy.
- [ ] Vercel's actual function bundle size and production deployment are verified.

## Known deployment gaps

- **Vercel production bundle size has not been verified here.** The API
  requirements have been split based on API imports and worker handlers, but the
  actual Vercel build/deployment output is still the authority for the 500 MB
  function cap.
- **The Rust processor has not been compiled in this sandbox.** Build it with
  `docker compose build processor` in an environment with Docker/Rust available
  before enabling it in production.
- **The Compose stack has not been run end-to-end in this sandbox.** The
  configuration is covered by static deployment tests, but Docker images,
  external worker connectivity, Redis/S3 behavior and production services need a
  real deployment smoke test.
- **PostgreSQL has not been exercised in this environment.** The baseline
  migration, `alembic check`, upgrade and downgrade were verified against SQLite;
  the CI/container environment that has PostgreSQL should run
  `alembic upgrade head` against a real instance before the first production
  deployment.
- **The browser E2E suite requires a separately installed Playwright browser**
  and has not been run in this change unless stated in the implementation report.

## Local ASR without a hosted provider

`faster-whisper` weights are optional and are not bundled in the default worker
requirements. For local ASR, add/install the provider and model weights on the
worker, then set `AI_TRANSCRIPTION_PROVIDER=faster-whisper` and configure
`WHISPER_DOWNLOAD_ROOT` (or pre-cache the model with
`WHISPER_LOCAL_FILES_ONLY=true`). Otherwise configure a hosted transcription
provider or paste a script and use real DTW alignment. Capabilities report which
provider the current deployment can use.
