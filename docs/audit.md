# ClipForge — engineering audit and implementation report

**Repository:** `Obiorachibuike/ClipForge`
**Audited commit:** `04fa004` (`main`) plus the changes on
`arena/3a0d3e4e-clipforge`
**Date:** 2026-10-20
**Environment:** Linux sandbox, Python 3.11.2, Node 22.22.3, FFmpeg 7.0.2
(imageio-ffmpeg static build), SQLite. No Docker, PostgreSQL, Redis, S3 or
Playwright browser is available here.

This report distinguishes four things that are easy to confuse:

1. **Implemented** — the code path exists and is wired into the product.
2. **Unit/API tested** — exercised by the test suite without external media.
3. **Verified with real media** — a real generated MP4 with real speech ran
   through the pipeline in this environment.
4. **Verified in a deployed environment** — Docker/Vercel/PostgreSQL/Redis/S3.
   **None of these were available, so none are claimed.**

Every number below comes from a command that was actually run. Where a check
could not run, the table says `NOT RUN` and the blocker is listed.

---

## 1. Executive summary

The application is not a mock: uploads, probing, transcription, script
alignment, candidate discovery, face-tracked framing, ASS captions, FFmpeg
rendering, preview streaming and export download are all implemented and were
exercised end to end with a real 10.6 MB, 145 s video.

The audit found **one critical cross-tenant data-exposure defect**, five high
severity defects, seven medium and several low issues. All critical, high and
medium findings were fixed in this change and covered by regression tests,
except the deployment-environment gaps listed in §8.

The most consequential finding was that a caller could pass **another user's
`video_id`** to `POST /projects/{id}/discover`. The queued job then used that
video and wrote candidates into the caller's project; candidate payloads carry
the transcript text, so this exposed another account's content. The endpoint
now rejects unknown fields (`422`), the job refuses a video that does not
belong to the project, and untrusted numeric settings are clamped.

Database migrations had no baseline at all. An Alembic environment and a
22-table initial revision were added and verified: fresh upgrade, `alembic
check` (no drift), and downgrade to base.

---

## 2. Findings

Severity reflects impact if the defect is exploited or triggered: **Critical** =
cross-tenant data access or trivial takeover; **High** = denial of service,
data loss, or unsafe defaults; **Medium** = incorrect output or a meaningful
security weakness; **Low** = quality, observability or dead code.

| ID | Sev. | Finding | Evidence | Status |
| --- | --- | --- | --- | --- |
| C1 | Critical | Discovery accepted a foreign `video_id`; the job then used that video and wrote its transcript content into the caller's project | Live exploit: `POST /projects/{mine}/discover` with `{"video_id": "<victim>"}` returned **202** and the queued job carried the victim's id; the schema now returns **422** | **Fixed** |
| H1 | High | `InMemoryJobQueue.release()` deadlocked: it held the condition lock and called `enqueue()`, which re-acquires the same non-reentrant lock | Reproduced: enqueue → claim → release hung; `timeout 15` → exit **124**. No in-tree callers at the time, so it was latent | **Fixed** (RLock) |
| H2 | High | A transient Redis error escaped `worker.run_forever`; one queue blip terminated the worker process | `claim()` raised instead of `QueueUnavailable`; no retry loop around the claim | **Fixed** (backoff + `QueueUnavailable`) |
| H3 | High | Upload chunk endpoint buffered the entire request body before checking its size | `await request.body()` read the whole stream into memory before the size check, so any authenticated client could force unbounded buffering | **Fixed** (bounded stream + 413) |
| H4 | High | Production started happily with the documented placeholder `SECRET_KEY` | `_KNOWN_INSECURE_SECRETS` check absent; sessions and provider-key encryption derive from it | **Fixed** (fail-closed startup) |
| H5 | High | No migration baseline; schema was `create_all` only | No `alembic/` directory existed | **Fixed** (22-table baseline) |
| M1 | Medium | Clip creation accepted negative, NaN/inf or past-the-end ranges | `snap_and_validate` had no finite/range guard | **Fixed** |
| M2 | Medium | A failed transcription left the row `RUNNING`, and every retry created a new transcript | No failure-state persistence; `_prepare_transcript` always inserted | **Fixed** |
| M3 | Medium | When a provider returned segment timings only, word times were interpolated and presented as measured | `_synthetic_words` fabricated word positions | **Fixed** (honest segment cues) |
| M4 | Medium | A render could be saved without checking that its duration matched the requested range | `output_duration` was recorded but never compared | **Fixed** (verified output duration) |
| M5 | Medium | Crop expressions embedded keyframes to the end of the *source*, not the clip | A 40 s clip of a long video produced thousands of unreachable nested `if()`s | **Fixed** (clip-window keyframes) |
| M6 | Medium | WebSocket origin check used `endswith`, so `https://example.com.evil.com` and `https://evil-example.com` passed for `https://example.com` | `origin.endswith(url.split("//")[-1])` | **Fixed** (exact/same-host) |
| M7 | Medium | Non-media uploads were only logged; a PDF or HTML page renamed `.mp4` was accepted | `upload.unrecognised_container` warning and continue | **Fixed** (reject conclusively non-media) |
| L1 | Low | Missing export file produced an unhandled storage error | `storage.stat` raise path | **Fixed** (404 `export_file_missing`) |
| L2 | Low | Candidate windows could open on a 1–2-word sentence tail; titles ended in "…and you" | Observed in the real run: candidate started at `"noise."`, title ended `"clever and you"` | **Fixed** |
| L3 | Low | Unused, malformed `_UNSAFE_KEY` regex in `storage/base.py` | Dead constant | **Fixed** (removed) |
| L4 | Low | `redis.unavailable` is logged every 5 s while Redis is absent | Observed in worker logs | **Open** (noisy but correct fallback) |
| L5 | Low | WebSocket ticket consumption was GET-then-DELETE (two round trips) | `tickets.py` | **Fixed** (MULTI GET+DEL) |
| L6 | Low | `RedisJobQueue.active_since()` returns `{}` | Dead introspection API | **Open** |
| L7 | Info | Aligned words carry a constant confidence (0.354) | Provider limitation, not a pipeline assumption | Documented |

### C1 — cross-tenant video reference in discovery

Before the fix, discovery took `video_id` from the request body and used it in
the job without checking that the video belonged to the project. A victim's
transcript text is then returned to the attacker through the attacker's own
candidate list.

The fix has three layers, each independently tested:

* `DiscoverClipsRequest` forbids unknown fields (so `video_id` is `422`).
* `discover_job.run` resolves the project's own video and raises
  `NotFoundError` for anything else.
* `max_candidates`, `min_duration` and `max_duration` are clamped server-side
  (`MAX_CANDIDATES_LIMIT = 40`, floor 5 s, ceiling 180 s) instead of trusted.

Live re-test after the fix:

```
{'video_id': '42867...'}                          -> 422 validation_error "Extra inputs are not permitted"
{'video_id': '42867...', 'max_candidates': 100000,
 'min_duration': -5}                              -> 422 validation_error "max_candidates: <= 40"
{}                                                -> 202 project.discover_clips
```

Tests: `test_discovery_rejects_extra_identifiers_instead_of_merging_them`,
`test_discovery_job_refuses_a_video_from_another_project`,
`test_discovery_clamps_untrusted_duration_settings`.

### H1 — queue deadlock

The deadlock was real (exit 124, process wedged). It is still worth noting that
`release()` had no call sites in `apps/` or `scripts/`, so no production path
was hanging; the fix removes a trap for the next retry feature. The regression
test requeues a claimed job and asserts a second worker can claim it, with a
thread timeout so a regression fails instead of hanging the suite.

### H3 — upload buffering

The chunk endpoint now computes the exact expected byte count from the upload
session **before** reading anything, rejects a lying or oversized
`Content-Length` with `413 payload_too_large`, and streams at most that many
bytes. A chunk that is one byte too large is rejected without storing it
(`test_oversized_chunk_is_rejected_before_being_buffered`).

### H4 — production secrets

`validate_runtime_settings()` runs at API and worker startup. In
`ENVIRONMENT=production` it refuses to start when `SECRET_KEY` or `JWT_SECRET`
is the documented placeholder, shorter than 32 characters, or empty. It logs
non-fatal risks (`COOKIE_SECURE=false`, `AUTO_CREATE_TABLES=true`,
`STORAGE_BACKEND=local`, empty CORS). Development and tests are unaffected.

### H5 — migrations

`apps/api/alembic/` now contains the environment and a single initial revision
(`7627644971dd`). Verified against a fresh SQLite database:

```
alembic upgrade head   -> 22 application tables created
alembic current        -> 7627644971dd (head)
alembic check          -> "No new upgrade operations detected."
alembic downgrade base -> only alembic_version remains
```

Table parity with the ORM: `migrated tables: 23 (incl. alembic_version),
models: 22, missing: [], extra: []`.

The API/worker Docker image now copies `alembic.ini` and `alembic/`, so the
deployed container can actually run the migration step.

---

## 3. Real-media verification

### 3.1 Asset

`scripts/make_demo_asset.py` synthesises speech with espeak-ng (one utterance
per sentence, real pauses) and muxes it over generated motion; nothing is
simulated downstream. Result: `demo/creator_masterclass.mp4`, 10.6 MB,
1920×1080 @ 30 fps, 145.3 s of narration.

### 3.2 Full pipeline (seed runner, same services as the API)

Command:

```bash
DATABASE_URL="sqlite:///$PWD/.data/seed-demo/clipforge.db" \
STORAGE_LOCAL_ROOT="$PWD/.data/seed-demo/storage" \
./.venv/bin/python scripts/seed.py --user \
  --asset demo/creator_masterclass.mp4 \
  --narration-script demo/narration.txt --process
```

Result: **PASS** in 153 s (probe 0.3 s, transcribe 73.2 s, framing 43.8 s,
discover 0.6 s, render 34.5 s).

| Stage | Measured result |
| --- | --- |
| Probe | 1920×1080, 30 fps, audio present, duration 145.3 s |
| Transcript | `espeak-dtw-alignment`, 63 segments / 409 words / 142.41 s, `has_word_timings=true` |
| Framing | detectors yunet/mediapipe unavailable → `haar`; 291 frames, 386 faces, 9 tracks, strategy `detected_face` |
| Discovery | analyzer `structural-v1` (no LLM configured), 4 non-overlapping candidates, scores 49/47/46/46 |
| Render | 1080×1920 H.264/AAC, 39.27 s, 8.0 MB, 34.5 s wall time |

### 3.3 Source-timestamp correctness

The rendered audio was decoded with PyAV and cross-correlated against the
source. This is the check that distinguishes "produced a file" from "cut the
right moment".

| Clip | Selected range | Correlation | Wrong-offset control |
| --- | --- | --- | --- |
| `eab600fd` | 0.00 – 39.27 s | **0.9978**, lag 0.000 s | t=100 s → 0.001 |
| `b2dbb1dd` | 93.407 – 132.32 s | **0.9957**, lag ≤ 0.001 s | ±2/5/10 s → ≈0.000 |

A second clip beginning at a non-zero source offset was rendered specifically
to prove seeking is not accidentally always starting at 0.

### 3.4 Full workflow over HTTP

A real server was started against the seeded database with
`WORKER_INLINE=true` (no Redis is available for an external worker).

| Step | Result |
| --- | --- |
| `POST /auth/login` | 200, session cookie + CSRF cookie |
| `GET /readyz` | 200 |
| `POST /projects/{id}/clips` (30.0 → 48.0) | **201**; snapped to real word boundaries 28.6 → 48.86, 20.26 s |
| `GET /clips/{id}/captions` | 200, 9 cues from the transcript |
| `POST /clips/{id}/render` | **202**, job queued |
| `GET /jobs/{id}` polling | `running` with real progress → `succeeded` |
| Render result | 1080×1920, AAC, **20.267 s**, 3,901,716 bytes |
| `GET /renders/{id}/download` with `Range: bytes=0-1023` | **206**, `Content-Range: bytes 0-1023/3901716`, 1024 bytes |
| Full download | **200**, `video/mp4`, 3,901,716 bytes |
| `GET /projects/{id}/exports` | new entry `http-smoke-clip-aad193.mp4`, 3,901,716 bytes |

This demonstrates ingest → clip → render → preview → download through the
public API, with the caveat that the worker ran in the API process because
Redis was unavailable.

---

## 4. Implementation status by feature

| Capability | Implemented | Unit/API tested | Real media | Deployed env |
| --- | --- | --- | --- | --- |
| Chunked / resumable upload | Yes | Yes | Yes (3 chunks of 4 MiB) | No |
| Single-request upload with bounded streaming | Yes | Yes | Via seed | No |
| Signed direct-to-S3 upload | Yes | No | No (S3 absent) | No |
| Container probe (PyAV → ffprobe → ffmpeg) | Yes | Yes | Yes | No |
| Transcription: script DTW alignment | Yes | Yes | Yes | No |
| Transcription: faster-whisper / hosted | Yes (provider interface) | No | No (models/keys absent) | No |
| Speaker clustering / turns | Yes | Yes | Yes (acoustic) | No |
| Sentence construction | Yes | Yes | Yes | No |
| Candidate discovery + scoring + dedup | Yes | Yes | Yes (structural) | No |
| LLM scoring blend / copywriting | Yes | Yes (unit) | No (no key) | No |
| Source-timestamp mapping | Yes | Yes | **Yes (0.9957)** | No |
| Smart framing (Haar; YuNet/MediaPipe optional) | Yes | Yes | Yes (9 tracks; crop not visually inspected) | No |
| ASS captions + styles | Yes | Yes | Yes (112 events generated) | No |
| Burned-in caption pixels | Yes | Via render | Not OCR-verified | No |
| Headline overlay | Yes | Yes | Spec generated; not OCR-verified | No |
| FFmpeg render + progress + timeout | Yes | Yes | Yes | No |
| Durable storage | Local + S3 interfaces | Yes | Local | No |
| Preview with HTTP Range | Yes | Yes | **Yes (206)** | No |
| Export centre + download | Yes | Yes | Yes | No |
| Auth: cookie sessions, CSRF, ownership | Yes | Yes | Yes (HTTP) | No |
| Cross-user isolation | Yes | Yes | Yes (422/404) | No |
| Durable queue + retries + recovery | Yes | Memory backend + failure modes | Memory only | No |
| WebSocket job events | Yes | Yes | Not exercised in browser | No |
| Cost controls (plan limits, usage) | Yes | Yes | Partially exercised | No |
| Migrations | Yes | Yes (SQLite) | n/a | No (PostgreSQL) |
| Vercel deployment | Config + static tests | Yes | n/a | **No** |
| Docker image / Compose | Yes | Static tests | n/a | **No** |
| Rust processor | Source present | Sidecar tests | No build | No |
| Frontend workflow | Yes | 110 tests + build | No browser E2E | No |

---

## 5. Test results

All commands were run from the repository root unless noted. `Status` is
`PASS`, `FAIL`, `BLOCKED` or `NOT RUN`; `FAIL` entries are not "accepted
failures" — none remain.

| Check | Command | Actual result | Status |
| --- | --- | --- | --- |
| Backend suite (unit + API + real-media integration) | `./.venv/bin/python -m pytest apps/api/tests` | **158 passed**, 5 warnings, 35.3 s | PASS |
| Frontend unit tests | `cd apps/web && npm test` | **105 passed**, 8 files, 12.1 s | PASS |
| TypeScript | `cd apps/web && npx tsc --noEmit` | exit 0 | PASS |
| API/type parity | `cd apps/web && npm run check:api` | "20 interfaces compared" | PASS |
| Frontend production build | `cd apps/web && npm run build` | built in 11.5 s, 2009 modules | PASS |
| Alembic fresh upgrade | `alembic upgrade head` on an empty SQLite DB | 22 tables, revision `7627644971dd` | PASS |
| Migration model parity | compare `Base.metadata.tables` with the migrated DB | 22 models, missing [], extra [] | PASS |
| Migration drift | `alembic check` | "No new upgrade operations detected." | PASS |
| Migration rollback | `alembic downgrade base` | only `alembic_version` remains | PASS |
| Real-media pipeline (seed) | `scripts/seed.py … --process` | PASS 153 s; render 1080×1920, 39.27 s | PASS |
| Source-timestamp correlation | PyAV decode + cross-correlation | 0.9978 and 0.9957 at lag 0 | PASS |
| HTTP clip creation | `POST /projects/{id}/clips` | 201, snapped 28.6 → 48.86 | PASS |
| HTTP render + progress | `POST /clips/{id}/render`, poll `/jobs/{id}` | 202 → succeeded, 20.267 s | PASS |
| HTTP range preview | `GET /renders/{id}/download` with Range | 206, correct `Content-Range` | PASS |
| HTTP export download | `GET /projects/{id}/exports` | 200, 3,901,716-byte entry | PASS |
| Cross-tenant discovery exploit | live POST with foreign `video_id` | 422 (was 202) | PASS |
| Docker image build | `docker compose build` | no Docker binary in sandbox | NOT RUN |
| PostgreSQL migration | `alembic upgrade head` against PostgreSQL | no PostgreSQL server | NOT RUN |
| Redis queue integration | worker against Redis | no Redis server | NOT RUN |
| S3 storage integration | upload/download with MinIO/S3 | no S3 service | NOT RUN |
| Vercel deployment / bundle size | `vercel deploy` | no credentials/project link | NOT RUN |
| Playwright browser E2E | `npx playwright test` | browser not installed | NOT RUN |
| Rust processor build | `cargo build` / `docker compose build processor` | no Rust toolchain | NOT RUN |
| Load / soak testing | — | not attempted | NOT RUN |

The 158 backend tests include `test_media_pipeline.py`, which synthesises
speech, builds a real MP4, runs probe → transcribe → discover → create clip →
render, decodes the output with PyAV and asserts resolution, duration and audio
correlation against the selected source interval. It is marked `integration`
and can be excluded with `-m "not integration"`.

---

## 6. Database and infrastructure

### Verified

* SQLite schema created exclusively by Alembic matches the ORM exactly.
* Upgrade, drift check and downgrade all work.
* The Dockerfile ships the migration environment.
* Static deployment tests assert that every Compose env var is read by
  `Settings`, every Dockerfile `COPY` source exists, the Vercel rewrite order
  routes `/api` and `/ws` to the API and keeps the processor internal, and
  worker-only dependencies stay out of the Vercel API requirements.

### Not verified (and why it matters)

* **PostgreSQL.** Alembic ran on SQLite only. The revision uses
  `postgresql.JSONB` and `postgresql.UUID` variants; PostgreSQL type rendering
  and the `CREATE TYPE`/index statements must be exercised on a real server.
* **Redis.** The Redis queue, cancellation flags, event bus and WebSocket
  tickets are implemented; the fail-closed configuration and worker recovery
  paths are unit-tested, but no real Redis ran. Multi-process job delivery and
  the atomic ticket consumption are therefore unproven here.
* **S3/MinIO.** The S3 storage class is implemented but was not exercised; no
  object store was reachable, and local storage was used for all real-media
  runs.
* **Docker/Compose.** The stack was never built or started; Docker is absent.
* **Vercel.** The function-bundle limit (500 MB) and service bindings are
  configuration-tested only.

---

## 7. Environment configuration

The full reference is [`.env.example`](../.env.example). Values that change
behaviour materially:

| Variable | Default | Effect |
| --- | --- | --- |
| `ENVIRONMENT` | `development` | `production` enables fail-closed secret checks |
| `SECRET_KEY`, `JWT_SECRET` | placeholder | **must** be ≥32 random characters in production |
| `ENCRYPTION_KEY` | derived | Fernet key for stored provider credentials |
| `DATABASE_URL` | PostgreSQL | also drives Alembic |
| `AUTO_CREATE_TABLES` | `true` | production should run `alembic upgrade head` and set `false` |
| `QUEUE_BACKEND` | `auto` | production requires Redis; memory is rejected |
| `WORKER_INLINE` | `true` in `.env.example` | must be `false` on Vercel/multi-instance APIs |
| `STORAGE_BACKEND` | `auto` | `local` is not durable across hosts |
| `MEDIA_MAX_UPLOAD_BYTES` | 10 GB | per-file ceiling |
| `MEDIA_MAX_DURATION_SECONDS` | 14400 | post-probe ceiling for uploads and URL imports |
| `UPLOAD_CHUNK_SIZE` | 4 MiB | fits Vercel request limits |
| `AI_TRANSCRIPTION_PROVIDER` | `auto` | without a model/key, script alignment is the only real path |
| `AI_LLM_PROVIDER` | `auto` | absent → structural-only discovery |
| `VISION_FACE_DETECTOR` | `auto` | falls back to Haar; YuNet needs a model file |

Production startup emits warnings for `COOKIE_SECURE=false`,
`AUTO_CREATE_TABLES=true`, local storage and an empty CORS list.

---

## 8. Remaining production blockers

1. **No deployed-environment verification.** Docker, PostgreSQL, Redis, S3,
   Vercel and the Rust processor have not run. The code is written for them,
   but that is not the same as proof.
2. **ASR without a script is unavailable in this environment.** No Whisper
   weights and no hosted key: transcription only works when a narration script
   is supplied (real DTW alignment). The capability endpoint reports this
   honestly rather than inventing a transcript.
3. **Discovery is structural-only without an LLM key.** Scores are measured
   from text, timing and audio energy; the LLM blend and authored titles are
   implemented but unexercised.
4. **Vision is Haar-only here.** YuNet requires `VISION_YUNET_MODEL`;
   MediaPipe is not installed. Haar finds faces but tracks them less robustly.
5. **Vercel bundle size is unmeasured.** Worker-only packages were split out
   of `apps/api/requirements.txt`, but only a real Vercel build can confirm the
   500 MB function limit.
6. **Browser E2E has not run.** The frontend is unit-tested, type-checked and
   production-built; Playwright needs a browser binary.
7. **Multi-instance realtime depends on Redis.** Without it, events stay inside
   one process; the UI resynchronises on reconnect instead of going silently
   stale.

---

## 9. Prioritised next actions

1. Run `alembic upgrade head` against a real PostgreSQL instance and verify
   JSONB/UUID columns, constraints and indexes; then set
   `AUTO_CREATE_TABLES=false`.
2. Bring up Compose with Redis and MinIO; run a worker in its own container and
   repeat the probe → transcribe → discover → render workflow. Kill the worker
   mid-render and confirm `recover_stale` requeues it.
3. Deploy to Vercel and measure the API function bundle; confirm `/api` and
   `/ws` rewrites and the processor binding.
4. Install `faster-whisper` (or configure a hosted ASR key) and an LLM key;
   compare structural-only discovery with the blended path on the demo asset.
5. Add provider-credential encryption verification with a real
   `ENCRYPTION_KEY` rotation test.
6. Run the Playwright suite against the deployed stack (upload, analyze,
   editor, export, reconnect).
7. Add object-lifecycle cleanup for deleted clips/exports and a retention
   policy for orphaned upload parts.
8. Instrument queue depth, job failure rate, render duration and provider cost;
   alert on sustained `redis.unavailable` (currently logs every 5 s) and on
   workers that stop heart-beating.

---

## 10. Honest limitations of this report

* All performance numbers are from a shared sandbox CPU and are indicative, not
  benchmarks.
* PostgreSQL, Redis, S3, Docker, Vercel and the Rust processor were unavailable;
  every statement about them is configuration-level, not runtime-verified.
* Captions and headlines were verified as generated ASS events and as a render
  warning/spec, not by OCR of the output frames; a human visual check is still
  worthwhile.
* The real-media integration test uses espeak-generated speech, not a human
  recording. It proves timing, mapping and encoding, not ASR accuracy.
* The `demo/manifest.json` ground truth is regenerated with the asset; the
  checked-in file is restored after regeneration to avoid unrelated diffs.
