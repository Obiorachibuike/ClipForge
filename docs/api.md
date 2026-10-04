# API

Base path `/api/v1`. Interactive documentation: `/docs` (Swagger UI) and
`/openapi.json`.

## Authentication

Sessions are HttpOnly cookies — no token is ever returned to JavaScript, and
nothing belongs in `localStorage`.

```bash
# Register (sets the cookies, returns the user + CSRF token)
curl -c jar.txt -X POST http://localhost:8000/api/v1/auth/register \
  -H 'content-type: application/json' \
  -d '{"email":"you@example.com","password":"sup3rsecret1","name":"You"}'

# Sign in
curl -c jar.txt -X POST http://localhost:8000/api/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"you@example.com","password":"sup3rsecret1"}'

# Who am I (also refreshes the CSRF cookie)
curl -b jar.txt http://localhost:8000/api/v1/auth/session
```

**Every non-GET request needs the CSRF header.** The token is in the
`clipforge_csrf` cookie (deliberately readable by JS) and in the
`csrf_token` field of the session response; send it as `X-CSRF-Token`:

```bash
CSRF=$(python3 -c "import json;print(json.load(open('session.json'))['csrf_token'])")
curl -b jar.txt -X POST http://localhost:8000/api/v1/projects \
  -H "content-type: application/json" -H "x-csrf-token: $CSRF" \
  -d '{"name":"Launch video"}'
```

Only the header is accepted — a cookie-only check would provide no CSRF
protection at all. A failure is `403` with `code: "csrf_failed"`.

Programmatic clients may send `Authorization: Bearer <session-cookie-value>`
instead of the cookie, but the CSRF rule still applies.

## Errors

Every failure has the same shape:

```json
{
  "code": "validation_error",
  "message": "Files must be smaller than 10.0 GB on this deployment.",
  "details": { "field": "size_bytes" },
  "request_id": "0ca7f9bb882545708d4f1f0d95da31ae"
}
```

| Status | Meaning |
| --- | --- |
| 400 / 422 | The request or its content is invalid. `details.field` names the offender. |
| 401 | Not signed in, or the session expired. |
| 403 | Authenticated but refused (CSRF failure, admin-only route). |
| 404 | Not found **or** not yours — deliberately indistinguishable. |
| 409 | Conflicting state (duplicate resource, cancelled job). |
| 402 | Plan limit exceeded. |
| 429 | Rate limited; `Retry-After` and `details.retry_after` are set. |
| 502 / 503 | Upstream AI provider or storage failure. `details.retryable` says whether retrying helps. |

Stack traces are logged server-side and never returned. Quote the `request_id`
when reporting a problem.

## Pagination

List endpoints take `limit` (1–100, default 20) and `offset`:

```json
{ "items": [], "total": 0, "limit": 20, "offset": 0, "has_more": false }
```

## Endpoints

### Auth `/auth`
`POST /register`, `POST /login`, `POST /logout`, `GET /session`,
`POST /password/change`, `POST /password/reset-request`, `POST /password/reset`

### Projects `/projects`
`GET /` · `POST /` · `GET /{id}` · `PATCH /{id}` · `DELETE /{id}` ·
`POST /{id}/archive` · `POST /{id}/restore`

`ProjectSummary` includes video/clip/candidate/render counts, the latest video,
active jobs, and an aggregate `progress` + `stage`, so a dashboard needs one
request per project.

### Videos and uploads `/uploads`, `/videos`
```
POST /uploads/init        → upload_id, chunk_size, total_chunks, received_chunks
PUT  /uploads/{id}/chunk/{index}     raw bytes, 256 KiB – 64 MiB per chunk
GET  /uploads/{id}        → status, received_chunks (resume from these)
POST /uploads/{id}/complete          → { video, job }
DELETE /uploads/{id}                 → cancel and discard
```
`POST /projects/{id}/videos` accepts a single multipart file for small uploads.
`GET /videos/{id}/stream` is range-enabled; `POST /videos/{id}/signed-url`
returns a time-limited storage URL. Chunk uploads are idempotent by index, so a
retry after a dropped connection is always safe.

### Transcripts `/projects/{id}/transcript`, `/transcripts/{id}`
`POST /videos/{id}/transcribe` (optional `{ script }` for alignment),
`GET /transcripts/{id}` (with segments and words), `GET .../words`,
`GET .../window?start&end`, `GET .../search?q`, `POST .../retranscribe`,
`PATCH` word text edits (which never move timings).

### Candidates `/projects/{id}/candidates`, `/candidates/{id}`
`POST /projects/{id}/discover` creates a discovery job; `GET /candidates/{id}/score`
explains the score per signal; `POST /candidates/{id}/clip` turns a candidate into
a clip; `POST /projects/{id}/candidates/bulk` applies keep/reject decisions.

### Clips `/projects/{id}/clips`, `/clips/{id}`
Create from a candidate or manually, `PATCH` trim/crop/style, `POST /{id}/duplicate`,
`POST /{id}/keep`, `POST /{id}/reject`, `PATCH /{id}/crop` for a manual crop.
`GET /clips/{id}` returns the clip with its video, source candidate and latest
render.

### Captions `/clips/{id}/captions`
`GET` returns cues for the player, `PATCH` updates style and text overrides,
`POST /clips/{id}/captions/regenerate` rebuilds from the transcript.
`GET /caption-presets` lists the seven built-in looks; `GET/POST /caption-styles`
manage user styles.

### Headlines `/clips/{id}/headlines`
`GET` lists suggestions (LLM or extractive), `POST /generate` with `{ count }`,
`POST /select` to place one. Nothing is auto-placed: a clip only shows a headline
after an explicit selection.

### Renders and exports
`POST /clips/{id}/render` with a preset, `GET /renders/{id}`,
`POST /renders/{id}/cancel`, `POST /renders/{id}/retry`,
`GET /projects/{id}/renders`, `GET /render-presets`.
`GET /exports`, `GET /exports/{id}/download`, `GET /exports/{id}/thumbnail`,
`DELETE /exports/{id}`.

Presets: `tiktok`, `youtube_shorts`, `instagram_reels`, `facebook_reels`,
`generic_vertical` (all 1080×1920), `square` (1080×1080), `landscape` (1920×1080).

### Jobs `/jobs`
`GET /jobs` (filter by `status`, `type`, `project_id`), `GET /jobs/active`,
`GET /jobs/{id}`, `POST /jobs/{id}/cancel`, `POST /jobs/{id}/retry`.

Jobs are never processed inside the request. `result` holds stage output (for
example the render's output key and size), `error_code`/`error_message` explain
failures, and `attempts`/`max_attempts` show retry state.

### Account `/settings`, `/usage`, `/billing`
`GET/PATCH /settings`, `GET /capabilities`, `GET /usage`, `GET /usage/history`,
CRUD `/settings/ai-providers` (+ `POST /{id}/test`),
`GET /billing/plans` (**public**), `GET /billing/subscription`,
`POST /billing/checkout`, `POST /billing/cancel`.

### System
`GET /healthz`, `GET /readyz`, `GET /api/v1/system/status`,
`GET /api/v1/system/metrics`, `POST /api/v1/ws-ticket`.

## WebSocket

`GET /ws/v1/events` — authenticate with the session cookie or `?ticket=<value>`
from `POST /api/v1/ws-ticket`.

**Server → client**

| Type | Payload |
| --- | --- |
| `system.ready` | `connection_id`, `channels` |
| `system.resync` | `jobs` — current snapshot, sent on connect and on request |
| `system.subscribed` | `channels` |
| `system.heartbeat` | `{pong: true}` |
| `system.error` | `code`, `message` |
| `job.created` / `job.started` / `job.progress` / `job.completed` / `job.failed` / `job.cancelled` | `progress`, `stage`, `message`, ids |
| `clip.created`, `render.started`, `render.progress`, `render.completed`, `export.ready`, `transcript.ready`, `usage.updated` | resource ids |

**Client → server**

```json
{"action": "subscribe", "channels": ["project:<id>"]}
{"action": "unsubscribe", "channels": ["project:<id>"]}
{"action": "resync", "project_id": "<id>"}
{"action": "ping"}
```

Tickets are single-use and short-lived, so fetch a fresh one per reconnect. The
reference client (`apps/web/src/ws/client.ts`) does exactly that, and recycles a
socket that goes silent rather than showing stale progress forever.

## Capabilities

`GET /api/v1/capabilities` reports what the deployment can genuinely do:
FFmpeg/FFprobe presence, the active vision detector, whether local ASR is
installed, which LLM/embedding providers are configured, storage and queue
backends, and whether the Rust sidecar is reachable. The UI renders these
verbatim, so a missing feature is shown as missing rather than failing at the
moment of use.
