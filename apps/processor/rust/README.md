# ClipForge processor (Rust)

Optional, stateless, independently scalable media-orchestration sidecar.

**The web app never depends on it.** Every command exists in the Python pipeline
too, and `apps/api/app/services/processor.py` falls back to the in-process
implementation whenever this service is disabled or unreachable. Enable it when
you want probing, waveform scanning and frame extraction to run outside the API
process (or across several machines) — see `PROCESSOR_ENABLED`, `PROCESSOR_URL`
and `PROCESSOR_BINARY` in `.env.example`.

## Build

```bash
cargo build --release            # target/release/clipforge-processor
docker build -f apps/processor/rust/Dockerfile -t clipforge-processor .
```

## Two transports, one contract

The client (`app/services/processor.py`) speaks to the sidecar either over HTTP
when it runs as a service, or by executing the binary per task when it lives on
the same host. Both carry the same JSON, and both must return
`Result.data` with `ok: true`.

### HTTP (`serve`)

```bash
clipforge-processor serve 0.0.0.0:8100
```

| Endpoint | Body | Response |
| --- | --- | --- |
| `GET /healthz` | – | `{"status":"ok"}` — liveness |
| `GET /readyz` | – | `{"status":"ok","ffmpeg":true,...}`, `503` when FFmpeg is missing |
| `POST /v1/health` | `{}` | `{"ok":true,"service":...,"version":...,"ffmpeg":bool,"ffprobe":bool}` |
| `POST /v1/probe` | `{"path":"/abs/file.mp4"}` | `{"ok":true,"data":{duration,width,height,fps,video_codec,audio_codec,audio_channels,audio_sample_rate,has_audio,rotation,container,bitrate,nb_frames,streams}}` |
| `POST /v1/waveform` | `{"path":"...","buckets":400}` | `{"ok":true,"peaks":[0.0..1.0,...]}` — exactly `buckets` values |
| `POST /v1/frames` | `{"path":"...","output_dir":"...","fps":1.0,"width":320,"max_frames":300}` | `{"ok":true,"count":N,"frames":[{"index","time_seconds","path","bytes"}]}` |

Failures return `422` with `{"ok":false,"error":"a sentence a user could act on"}`.

The probe response is normalized to the same shape
`apps/api/app/services/media/ffmpeg.py` produces, so callers cannot tell which
implementation answered.

### CLI

```bash
echo '{"path":"/abs/file.mp4"}' | clipforge-processor probe
```

One JSON object on stdin, one JSON object on stdout. A non-zero exit code means
the client reads `stderr` as the failure detail.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `PROCESSOR_BIND` | `0.0.0.0:8100` | Listen address for `serve` |
| `PROCESSOR_FFMPEG` | `ffmpeg` (PATH) | FFmpeg binary |
| `PROCESSOR_FFPROBE` | `ffprobe` (PATH) | FFprobe binary |
| `RUST_LOG` | `info` | `tracing` filter |
| `LOG_JSON` | `false` | Emit JSON log lines |

## Security

The service has **no authentication** and executes FFmpeg against paths it is
given; it is designed to sit on an internal Docker network. Do not expose it to
the internet. The API only ever passes paths inside job workspaces it created
itself (`safe_output_path`), never user-supplied strings.

## Verification status

The crate is written against the documented contract, but **it has not been
compiled or run in the development sandbox that produced it**: the environment
has no Rust toolchain and the toolchain download hosts are blocked, so there is
no way to execute `cargo build` there. `docker compose build processor` is the
first real compile — expect to fix any compiler diagnostics at that point. The
Python side that consumes it *is* tested (`apps/api/tests/test_processor.py`
covers the client, including the fallback path).
