# Architecture

ClipForge turns a long video into short-form clips. This document explains how
the pieces fit, and — where it matters — why they were built this way.

## Components

```
                    ┌──────────────┐
   browser ───────▶ │  web (nginx) │  SPA + reverse proxy (single origin)
                    └──────┬───────┘
                           │ /api, /ws
                    ┌──────▼───────┐        ┌──────────────┐
                    │   api        │◀──────▶│  postgres    │  metadata only
                    │  (FastAPI)   │        └──────────────┘
                    └──┬────────┬──┘        ┌──────────────┐
                       │        └──────────▶│   redis      │  queue, pub/sub, cache
                       │                    └──────┬───────┘
                       │                           │
                       │                    ┌──────▼───────┐
                       │                    │   worker     │  same image, no HTTP
                       │                    └──┬────────┬──┘
                       │                       │        │
              ┌────────▼────────┐   ┌──────────▼──┐  ┌──▼─────────────┐
              │ minio / S3 / R2 │   │  processor  │  │ ffmpeg + opencv│
              │  video objects  │   │   (Rust)    │  │  in worker     │
              └─────────────────┘   └─────────────┘  └────────────────┘
```

| Component | Responsibility |
| --- | --- |
| `web` | React SPA. Serves static assets, proxies `/api`, `/ws`, `/healthz` to the API so the browser only ever talks to one origin. |
| `api` | FastAPI. Auth, uploads, CRUD, job creation, WebSocket fan-out. **Never** processes media inline. |
| `worker` | Runs jobs from the queue. Same image as the API, different command, so they cannot drift. |
| `postgres` | All metadata: users, projects, videos, transcripts, candidates, clips, captions, jobs, renders, exports, usage. No video binaries. |
| `redis` | Job queue, pub/sub for WebSocket fan-out, rate-limit counters, upload sessions. |
| `minio` | S3-compatible object storage for the video/audio/JPEG objects. Swappable for R2 or real S3. |
| `processor` | **Optional** Rust sidecar. Fast probing, waveform scans, parallel frame extraction. |

## The job model

Every long operation is a row in `jobs`, never a long-lived HTTP request.

1. A request creates a `Job` (`JobService.create`) and enqueues its id.
2. The API returns immediately with the serialized job.
3. A worker picks it up, streams progress onto the row, and emits an event.
4. The event bus fans the event out to subscribed WebSocket connections.

Properties that follow from this design:

- **Crash recovery.** A worker that dies mid-job leaves a `running` row with a
  stale heartbeat; `JobService.recover_stale` re-queues it.
- **Cancellation.** `POST /jobs/{id}/cancel` sets `cancel_requested`; handlers call
  `JobService.check_cancelled` between stages, so work stops promptly without
  killing a process.
- **Retries.** Failures increment `attempts` up to `max_attempts` with backoff.
- **Ownership.** Jobs carry `user_id`/`project_id`; every read and cancel is
  scoped to the caller.

Progress is written to the database, so a client that reconnects, refreshes the
page, or never had a WebSocket at all can recover full state from REST.

## The pipeline

`upload → probe → transcribe → discover → (review) → clip → render → export`

Each stage is a separate job type with its own handler in
`app/services/pipeline/`. Handlers are plain functions taking a session and a
job, which keeps them testable and lets a job be re-run in isolation.

### Transcription

`faster-whisper` produces word-level timestamps. Word timings are stored in
`transcript_words` and carried through the entire pipeline — clips, captions and
exports are all word-accurate, which is what makes word-synchronized captions and
click-a-word-to-seek possible.

When no ASR model is available (no weights, no GPU, offline), the pipeline can
**align a known script against the audio** with DTW over espeak-ng phonemes
(`services/ai/transcription/alignment.py`). That is a real measurement of the
audio, not a guess. The transcript records
`script_alignment {applied, quality_ratio, confidence}`, and a quality gate
rejects alignments worse than `MAX_ALIGNMENT_QUALITY_RATIO` rather than shipping
bad timings.

### Clip discovery

`services/pipeline/analyzer.py` scores every candidate window on measurable
signals: hook strength, self-containedness, insight/payoff language, surprise,
emotion, dynamics (energy variance), novelty (embedding distance from the
corpus), length fit, story markers and confidence. Weights live in one auditable
`DEFAULT_WEIGHTS` dict. An LLM, when configured, blends into 40% of the final
score — but the structural score stands on its own, and the analyzer records
whether an LLM was used (`llm_used`).

Nothing is fabricated: the same transcript always yields the same scores, and
`apps/api/tests/test_units.py` pins that property.

### Smart framing

Priority: active speaker → face → multiple faces → main subject → center.
OpenCV (Haar cascades + saliency) detects faces per sampled frame; keyframes are
stored **in source pixel coordinates** and scaled per output preset at render
time. Smoothing avoids jitter, and the crop is clamped so faces are never cut off
or given inconsistent headroom. The crop can be overridden per clip.

### Captions

ASS subtitle generation (`services/media/ass.py`) because the bundled static
FFmpeg has no `drawtext`. Seven presets — Classic, Bold, Minimal, Podcast,
Karaoke, Creator, High Contrast — with full control over position, font, size,
weight, colour, background, stroke, shadow, highlight, animation, and max
words/characters per line. Text edits rewrite caption text **without touching
timings**, so synchronization is preserved.

### Rendering

FFmpeg with one `-filter_complex` graph: crop/scale to the target aspect,
burn in captions, overlay the headline, mix audio, and encode H.264/AAC with
`+faststart`. Output is written to a job workspace and uploaded to storage.
Renders record their exact `spec` so a render is reproducible.

## Storage

`StorageBackend` (`base.py`, `local.py`, `s3.py`) is provider-independent: local
filesystem for development, S3/R2/MinIO for deployments. Videos are uploaded
chunked and resumable (`UploadService`), spooled to storage without ever
buffering a whole file in memory or in the database. Streaming playback uses
range requests against signed storage URLs.

`storage_backend=auto` picks S3 only when an endpoint and credentials are
configured **and** a bucket probe succeeds; otherwise it stays local. That probe
is what stops a misconfigured deployment from silently writing to the wrong
place.

## AI providers

All model access goes through interfaces in `services/ai/`:

- transcription: local faster-whisper, OpenAI-compatible, custom
- LLM: OpenAI-compatible, Gemini, Anthropic, custom, Ollama
- embeddings: sentence-transformers, TF-IDF fallback

`registry.py` resolves the best available provider for each task, and
`capabilities.py` reports truthfully what exists. When nothing is configured the
system says so and degrades to structural analysis — it never invents a result.

Providers are selected per user: a user can register their own API keys, which
are encrypted at rest (Fernet) and only ever returned masked.

## The Rust processor

`apps/processor/rust` is an optional sidecar for CPU/disk-bound work — probing,
waveform envelope scanning, and parallel frame extraction. It is a *pure
orchestrator*: it shells out to FFmpeg rather than linking `libav`, so its build
has no native media dependencies and both implementations agree on media
handling down to the encoder version.

**The web app never depends on it.** `services/processor.py` falls back to the
Python implementation whenever the sidecar is disabled, unreachable, slow or
returning nonsense, and the responses it *does* accept are normalized and
validated against the same shape the Python prober produces. `/readyz` reports
sidecar reachability but never degrades readiness because of it.

The sidecar is stateless, so it scales horizontally with no coordination.

## Security

- **Sessions.** HttpOnly, `SameSite`, secure cookies. No token ever reaches
  JavaScript, so `localStorage` holds no credential.
- **CSRF.** Double-submit: the browser reads a non-HttpOnly cookie and echoes it
  in `X-CSRF-Token`. Only the *header* is accepted, compared against the
  server-side session token — cookie-only checks are no protection at all.
- **Authorization.** Every resource read and mutation goes through an ownership
  check that returns 404 (not 403) for another user's objects, so object ids
  cannot be probed for existence.
- **Uploads.** Extension and MIME allowlists, a minimum and maximum size, and
  path-traversal sanitization; filenames never determine storage paths.
- **FFmpeg.** Arguments are built as a list (never a shell), numeric parameters
  pass through validators, expression values are escaped for the filtergraph,
  and output paths are confined to the job workspace by `safe_output_path`.
- **Secrets.** Provider keys encrypted at rest; passwords bcrypt-hashed; reset
  tokens stored as fingerprints, single-use and expiring.
- **Rate limits.** Separate buckets for global, authentication and upload
  traffic, keyed by user when known and IP otherwise.
- **Errors.** Structured `{code, message, details, request_id}` bodies. Stack
  traces go to the server log, never to the client.

## Realtime

`/ws/v1/events` authenticates with either the session cookie or a single-use
ticket (`POST /ws-ticket`, required for non-cookie clients). Clients subscribe to
channels (`user:<id>`, `project:<id>`, `job:<id>`); subscribing to a project the
caller does not own silently drops the channel rather than leaking its existence.

The client (`src/ws/client.ts`) reconnects with exponential backoff and jitter,
fetches a **fresh ticket per reconnect**, heartbeats, and recycles a socket that
goes silent — browsers do not always report a dead connection. On every
(re)connect it requests a resync, and the server answers with the current job
snapshot, so a job that finished while the socket was down is reflected without a
page reload.

## Frontend

React + TypeScript + Vite, Tailwind, Zustand for local UI state, TanStack Query
for server state, Framer Motion for motion. Video bytes never enter React state
or a store: the player points at a streaming URL and uploads stream from a
`File` in chunks.
