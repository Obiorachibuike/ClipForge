# ClipForge

**Turn long videos into short-form content automatically.**

Upload a long video; ClipForge transcribes it with word-level timing, finds the
moments worth clipping, frames them for vertical or square delivery, captions
them word by word, and renders finished MP4s for TikTok, YouTube Shorts,
Instagram Reels, Facebook Reels or a generic vertical format.

Browser-first SaaS — React in the browser, FastAPI and background workers behind
it. No desktop runtime, no Electron, no Tauri.

## Quick start

```bash
cp .env.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(48))"   # -> SECRET_KEY
python3 -c "import secrets; print(secrets.token_urlsafe(48))"   # -> JWT_SECRET

docker compose up --build          # http://localhost:8080
```

One command starts the web app, API, worker, PostgreSQL, Redis, MinIO (object
storage) and the optional Rust processor sidecar.

Prefer to run it locally without Docker? See [docs/development.md](docs/development.md).

## How it works

```
upload → probe → transcribe → discover moments → review → edit → render → export
```

Every stage is a real background job with persisted progress, retry,
cancellation and failure recovery — nothing is processed inside an HTTP request,
and progress streams to the browser over a WebSocket that reconnects and resyncs.

### Runtime split

The Vercel FastAPI function installs only `apps/api/requirements.txt`: web,
validation, database, Redis, storage, security and provider-HTTP dependencies.
It validates requests, stores upload chunks in the configured object store and
queues jobs. Media/ingestion dependencies live in
`apps/api/requirements-worker.txt`; the Docker-compatible worker image installs
that full set and runs outside Vercel. `apps/api/requirements-dev.txt` adds test
tools for local checks.

For resumable uploads, the API stores each chunk as a separate object. On
completion it queues a probe job with the upload ID; the worker assembles the
source in its own workspace, writes it back through shared object storage, then
runs the existing probe, transcription, discovery, framing and render pipeline.
The API and worker share PostgreSQL, Redis and object storage—not local disk.
Production must use Redis (no process-memory queue fallback) and set
`WORKER_INLINE=false`; `WORKER_EXTERNAL=true` on the API reports the external
worker's media capabilities.

| Stage | What actually happens |
| --- | --- |
| **Upload** | Chunked and resumable, streamed to object storage. The whole video never enters browser memory or the database. |
| **Transcribe** | Word-level timestamps with per-word confidence and speaker, preserved end to end. |
| **Discover** | Candidates scored on measured signals — hook strength, self-containedness, payoff language, surprise, energy dynamics, novelty, story structure. |
| **Frame** | Active speaker → face → multiple faces → main subject → centre, with smoothing so the crop does not jitter or cut off faces. Manual override available. |
| **Caption** | Seven presets with full styling and word-level synchronization. Editing text never breaks timing. |
| **Render** | FFmpeg to H.264/AAC with `+faststart`, per platform preset, with reproducible render specs. |

## No fake functionality

This is the rule the project is built around: **if it is not implemented, it is
absent — never simulated.**

- No mocked AI results, no hard-coded clip suggestions, no simulated progress.
- Every processing state maps to a real backend job.
- `GET /api/v1/capabilities` reports truthfully what the deployment can run, and
  the UI shows the same answer. Missing ASR weights, no configured LLM, no
  FFmpeg — each is reported as missing.
- Clip scores come from real analysis of the transcript, and the same input
  always produces the same score (pinned by tests).
- Features that need credentials (hosted AI, online checkout) say so instead of
  pretending to work.

## Stack

**Frontend** React 18 · TypeScript · Vite · Tailwind · Zustand · TanStack Query ·
Framer Motion · Lucide · HTML5 video · WebSocket

**Backend** FastAPI · Pydantic · SQLAlchemy · PostgreSQL · Redis · external
Docker-compatible media workers · WebSockets

**Media and AI** FFmpeg · OpenCV · optional faster-whisper (word timings) ·
optional sentence-transformers ·

**Optional** Rust (Axum/Tokio/Serde/tracing) sidecar for probing, waveform
scanning and parallel frame extraction. Independently scalable and never
required — the Python pipeline covers every command it offers.

All AI access sits behind provider interfaces (local / OpenAI-compatible /
Gemini / Anthropic / custom), selected per task and per user. No provider is
hard-coded.

## Layout

```
apps/api/           FastAPI app, worker pipeline, split API/worker/dev requirements
apps/web/           React single-page app
apps/processor/rust Optional Rust media-orchestration sidecar
infrastructure/     Dockerfile for the API/worker image
scripts/            demo asset generator, seeding, API type check
docs/               architecture, API, deployment, development
docker-compose.yml  the whole stack
```

## Documentation

| Document | Contents |
| --- | --- |
| [docs/architecture.md](docs/architecture.md) | Components, job model, pipeline internals, security design |
| [docs/api.md](docs/api.md) | REST endpoints, auth and CSRF, errors, WebSocket protocol |
| [docs/deployment.md](docs/deployment.md) | Compose, configuration, scaling, production checklist, known gaps |
| [docs/development.md](docs/development.md) | Local setup, demo media, test suites, troubleshooting |

## Tests

```bash
./.venv/bin/python -m pytest apps/api/tests -q    # 95 backend tests
cd apps/web && npm test                            # 99 frontend tests
cd apps/web && npx tsc --noEmit && npm run check:api
```

`npm run check:api` fails the build when the hand-written frontend types drift
from the live OpenAPI schema.

## Verification status

The repository contains the real upload, probing, transcription, discovery,
framing, caption, rendering and export implementations. The API/worker dependency
and storage boundary described above is subject to the checks run for the
current change; it does not imply that a Vercel deployment or its 500 MB bundle
limit has been verified. The Playwright suite, Rust container build and full
Compose integration require their respective external tools/services. See the
"Known deployment gaps" section of [docs/deployment.md](docs/deployment.md) and
the implementation report for current check results.
