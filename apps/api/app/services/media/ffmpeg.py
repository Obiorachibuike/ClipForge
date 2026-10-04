"""FFmpeg/FFprobe integration.

Safety rules enforced here (never bypassed elsewhere):
  * commands are always argument lists - `shell=True` is never used
  * every user-influenced value passes a validator before reaching an argument
  * filter strings are assembled from numbers and whitelisted enums only, or
    from files we generate ourselves inside the job workspace
  * child processes are registered with the runtime registry so cancellation
    can terminate them
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.errors import CancelledError_, MediaError
from app.core.logging import get_logger
from app.services.runtime import registry

log = get_logger(__name__)

_SAFE_FLOAT = re.compile(r"^-?\d+(\.\d+)?$")


class FFmpegError(MediaError):
    """FFmpeg exited non-zero. Carries the tail of stderr for the job log.

    The user-facing message is the *parsed* reason (see `_explain_stderr`); raw
    stderr stays in the job's `error_detail` for operators.
    """

    def __init__(self, message: str, *, stderr: str = "", returncode: int = 1) -> None:
        super().__init__(
            message,
            code="ffmpeg_failed",
            returncode=returncode,
            stderr_tail=stderr[-1500:],
        )
        self.stderr = stderr
        self.returncode = returncode


# --------------------------------------------------------------- validators ---
def num(value: Any, *, minimum: float | None = None, maximum: float | None = None, name: str = "value") -> str:
    """Validate a numeric value for use in an FFmpeg argument."""
    try:
        f = float(value)
    except (TypeError, ValueError) as exc:
        raise MediaError(f"Invalid numeric {name}.") from exc
    if math.isnan(f) or math.isinf(f):
        raise MediaError(f"Invalid numeric {name}.")
    if minimum is not None and f < minimum:
        f = float(minimum)
    if maximum is not None and f > maximum:
        f = float(maximum)
    text = f"{f:.6f}".rstrip("0").rstrip(".") if f != int(f) else str(int(f))
    return text or "0"


def intnum(value: Any, *, minimum: int | None = None, maximum: int | None = None, name: str = "value") -> str:
    try:
        i = int(float(value))
    except (TypeError, ValueError) as exc:
        raise MediaError(f"Invalid integer {name}.") from exc
    if minimum is not None:
        i = max(minimum, i)
    if maximum is not None:
        i = min(maximum, i)
    return str(i)


def choice(value: Any, allowed: set[str], *, default: str, name: str = "value") -> str:
    text = str(value or "").strip()
    if text in allowed:
        return text
    return default


def text_value(value: Any, *, max_length: int = 500) -> str:
    """Text that ends up inside a filter argument we generate. Newlines are
    stripped (they would break filter syntax) and control chars removed."""
    text = str(value or "")
    text = text.replace("\r", " ").replace("\n", " ")
    text = "".join(ch for ch in text if ch.isprintable() or ch == " ")
    return text[:max_length]


# Characters the FFmpeg filtergraph parser treats as syntax rather than data.
# Escaping these is what keeps a value from breaking out of the option it is
# interpolated into. The shell is never involved (`run_ffmpeg` passes argv as a
# list), so shell metacharacters such as `$` and `&` are harmless here.
_FILTERGRAPH_SPECIALS = ("\\", ",", ";", ":", "[", "]", "'")


def escape_expression(expression: str) -> str:
    """Escape an FFmpeg expression for use as a filter option value.

    Inside a filtergraph, `,` separates filters, `;` separates filter chains,
    `:` separates options and `[]` delimit link labels, so any of them inside a
    value must be escaped or FFmpeg rejects (or, worse, misparses) the graph —
    e.g. `if(lt(t,3),1,2)` needs its commas escaped.

    Escape order matters: the backslash must be doubled first, or the escapes
    added afterwards would themselves get escaped.
    """
    text = str(expression).replace("\r", "").replace("\n", "")
    text = text.replace("\\", "\\\\")
    for special in _FILTERGRAPH_SPECIALS[1:]:
        text = text.replace(special, f"\\{special}")
    return text


def escape_filter_path(path: str | Path) -> str:
    """Escape a filesystem path for use inside an FFmpeg filter argument.

    Backslashes become forward slashes first (Windows separators are never
    meaningful in a filtergraph), then every filtergraph metacharacter is
    escaped so a path containing `:` or `,` cannot split the filter options.
    """
    text = str(path).replace("\\", "/").replace("\r", "").replace("\n", "")
    for special in (":", "'", ",", ";", "[", "]"):
        text = text.replace(special, f"\\{special}")
    return text


def _explain_stderr(stderr: str) -> str:
    """Turn FFmpeg's stderr into one sentence a user can act on."""
    if not stderr:
        return ""
    lines = [line.strip() for line in stderr.strip().splitlines() if line.strip()]
    for line in reversed(lines):
        if line.startswith(("Error", "Invalid", "Conversion failed", "No such", "Option")):
            return line[:400]
    return lines[-1][:400] if lines else ""


def safe_output_path(path: Path, root: Path) -> Path:
    """Ensure an output path stays inside the job workspace."""
    resolved = Path(path).resolve()
    root = Path(root).resolve()
    if root not in resolved.parents and resolved != root:
        raise MediaError("Refusing to write outside the job workspace.", code="unsafe_path")
    return resolved


# ------------------------------------------------------------------ binaries ---
_bin_lock = threading.Lock()
_bin_cache: dict[str, str] = {}


def ffmpeg_bin() -> str:
    with _bin_lock:
        if "ffmpeg" not in _bin_cache:
            _bin_cache["ffmpeg"] = settings.ffmpeg_path
        value = _bin_cache["ffmpeg"]
    if not value:
        raise MediaError(
            "FFmpeg is not available on this server. Set MEDIA_FFMPEG_PATH or install ffmpeg.",
            code="ffmpeg_missing",
        )
    return value


def ffprobe_bin() -> str:
    with _bin_lock:
        if "ffprobe" not in _bin_cache:
            _bin_cache["ffprobe"] = settings.ffprobe_path
        return _bin_cache["ffprobe"]


def have_ffmpeg() -> bool:
    try:
        return bool(ffmpeg_bin())
    except MediaError:
        return False


def reset_binary_cache() -> None:
    """Test helper."""
    with _bin_lock:
        _bin_cache.clear()


# ------------------------------------------------------------------- runner ---
@dataclass
class FFmpegResult:
    returncode: int
    stderr: str = ""
    duration_seconds: float = 0.0
    progress_points: list[float] = field(default_factory=list)


def run_ffmpeg(
    args: list[str],
    *,
    job_id: str | None = None,
    total_duration: float | None = None,
    progress_cb: Callable[[float], None] | None = None,
    timeout: float | None = None,
    log_stderr: bool = True,
) -> FFmpegResult:
    """Run ffmpeg with `-progress pipe:1`, reporting real progress."""
    cmd = [ffmpeg_bin(), "-hide_banner", "-nostdin", *args]
    started = time.time()
    stderr_chunks: list[str] = []
    progress_points: list[float] = []
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    if job_id:
        registry.register_process(job_id, proc)

    def _drain_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_chunks.append(line)
            if len(stderr_chunks) > 4000:
                del stderr_chunks[:2000]

    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
    stderr_thread.start()

    try:
        if proc.stdout is not None:
            for raw in proc.stdout:
                line = raw.strip()
                if not line or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                if key in ("out_time_ms", "out_time_us") and total_duration:
                    try:
                        seconds = int(value) / 1_000_000
                    except ValueError:
                        continue
                    if seconds > 0:
                        pct = min(99.5, max(0.0, seconds / total_duration * 100.0))
                        progress_points.append(pct)
                        if progress_cb:
                            progress_cb(pct)
                elif key == "progress" and value == "end":
                    if progress_cb:
                        progress_cb(100.0)
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise MediaError("Media processing timed out.", code="ffmpeg_timeout")
    finally:
        try:
            if proc.stdout:
                proc.stdout.close()
        except Exception:
            pass
        stderr_thread.join(timeout=3)
        if job_id:
            registry.unregister_process(job_id, proc)

    stderr_text = "".join(stderr_chunks)[-8000:]
    result = FFmpegResult(
        returncode=proc.returncode or 0,
        stderr=stderr_text,
        duration_seconds=time.time() - started,
        progress_points=progress_points,
    )
    if result.returncode != 0:
        if registry.is_cancelled(job_id) if job_id else False:
            raise CancelledError_()
        if log_stderr:
            log.error("ffmpeg.failed", code=result.returncode, stderr=stderr_text[-1500:])
        raise FFmpegError(
            _explain_stderr(stderr_text) or f"FFmpeg exited with code {result.returncode}.",
            stderr=stderr_text,
            returncode=result.returncode,
        )
    return result


def run_ffmpeg_capture(args: list[str], *, timeout: float = 60.0) -> str:
    """Run a short ffmpeg command and return stdout (used for metadata parsing)."""
    cmd = [ffmpeg_bin(), "-hide_banner", "-nostdin", *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        raise MediaError("FFmpeg could not read this file.", code="media_unreadable")
    return proc.stdout


# -------------------------------------------------------------------- probe ---
def probe_media(path: Path) -> dict[str, Any]:
    """Extract container/stream metadata.

    Prefers PyAV (pure-python bindings to the FFmpeg libraries, no ffprobe
    binary required), then falls back to ffprobe, then to parsing ffmpeg -i.
    """
    errors: list[str] = []
    for strategy in (_probe_with_pyav, _probe_with_ffprobe, _probe_with_ffmpeg):
        try:
            data = strategy(Path(path))
            if data and data.get("duration", 0) >= 0:
                data["strategy"] = strategy.__name__
                return data
        except MediaError:
            raise
        except Exception as exc:  # try the next strategy
            errors.append(f"{strategy.__name__}: {exc}")
    log.error("probe.failed", path=str(path), errors=errors)
    raise MediaError("This file could not be read as a video or audio file.", code="media_unreadable")


def normalize_probe(data: dict[str, Any]) -> dict[str, Any]:
    """Coerce a probe document into the canonical shape used across the app.

    Public because the Rust sidecar's output is passed through it too, which is
    what lets either implementation answer without callers caring.
    """
    duration = float(data.get("duration") or 0.0)
    return {
        "duration": duration,
        "width": int(data.get("width") or 0),
        "height": int(data.get("height") or 0),
        "fps": float(data.get("fps") or 0.0),
        "video_codec": str(data.get("video_codec") or ""),
        "audio_codec": str(data.get("audio_codec") or ""),
        "audio_channels": int(data.get("audio_channels") or 0),
        "audio_sample_rate": int(data.get("audio_sample_rate") or 0),
        "has_audio": bool(data.get("has_audio")),
        "rotation": int(data.get("rotation") or 0),
        "container": str(data.get("container") or ""),
        "bitrate": int(data.get("bitrate") or 0),
        "streams": data.get("streams") or [],
        "nb_frames": int(data.get("nb_frames") or 0),
    }


def _apply_rotation(width: int, height: int, rotation: int) -> tuple[int, int]:
    if rotation in (90, 270, -90, -270):
        return height, width
    return width, height


def _probe_with_pyav(path: Path) -> dict[str, Any]:
    import av

    with av.open(str(path)) as container:
        data: dict[str, Any] = {
            "container": container.format.name if container.format else "",
            "duration": float(container.duration / av.time_base) if container.duration else 0.0,
            "bitrate": int(container.bit_rate or 0),
            "streams": [],
        }
        for stream in container.streams:
            if stream.type == "video" and not data.get("video_codec"):
                rate = float(stream.average_rate) if stream.average_rate else 0.0
                rotation = 0
                try:
                    rotation = int(stream.metadata.get("rotate", 0) or 0)
                except Exception:
                    rotation = 0
                data.update(
                    {
                        "video_codec": stream.codec_context.name or "",
                        "width": int(stream.codec_context.width or 0),
                        "height": int(stream.codec_context.height or 0),
                        "fps": rate,
                        "rotation": rotation,
                        "nb_frames": int(stream.frames or 0),
                    }
                )
                data["streams"].append({"type": "video", "codec": stream.codec_context.name, "fps": rate})
            elif stream.type == "audio" and not data.get("audio_codec"):
                rate = int(stream.codec_context.sample_rate or 0)
                layout = stream.codec_context.layout
                channels = int(len(layout.channels)) if layout and layout.channels else (int(stream.codec_context.channels or 0))
                data.update(
                    {
                        "audio_codec": stream.codec_context.name or "",
                        "audio_channels": channels,
                        "audio_sample_rate": rate,
                        "has_audio": True,
                    }
                )
                data["streams"].append({"type": "audio", "codec": stream.codec_context.name, "sample_rate": rate})
        if not data.get("duration"):
            # some containers (raw streams) omit it: fall back to stream duration
            for stream in container.streams:
                if stream.duration and stream.time_base:
                    data["duration"] = max(
                        float(data.get("duration") or 0.0), float(stream.duration * stream.time_base)
                    )
        if not data.get("fps") and data.get("duration"):
            frames = data.get("nb_frames") or 0
            if frames:
                data["fps"] = float(frames) / float(data["duration"])
        width, height = _apply_rotation(int(data.get("width") or 0), int(data.get("height") or 0), int(data.get("rotation") or 0))
        data["width"], data["height"] = width, height
        return normalize_probe(data)


def _probe_with_ffprobe(path: Path) -> dict[str, Any]:
    binary = ffprobe_bin()
    if not binary:
        raise RuntimeError("ffprobe not available")
    proc = subprocess.run(
        [
            binary,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=settings.media_probe_timeout_seconds,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-400:])
    payload = json.loads(proc.stdout or "{}")
    data: dict[str, Any] = {"streams": []}
    fmt = payload.get("format") or {}
    data["container"] = fmt.get("format_name", "")
    data["duration"] = float(fmt.get("duration") or 0.0)
    data["bitrate"] = int(float(fmt.get("bit_rate") or 0))
    for stream in payload.get("streams", []):
        if stream.get("codec_type") == "video" and not data.get("video_codec"):
            rate_text = stream.get("avg_frame_rate") or "0/1"
            try:
                numerator, _, denominator = rate_text.partition("/")
                fps = float(numerator) / float(denominator or 1)
            except Exception:
                fps = 0.0
            rotation = 0
            for side in stream.get("side_data_list") or []:
                if "rotation" in side:
                    try:
                        rotation = int(float(side["rotation"]))
                    except Exception:
                        rotation = 0
            data.update(
                {
                    "video_codec": stream.get("codec_name", ""),
                    "width": int(stream.get("width") or 0),
                    "height": int(stream.get("height") or 0),
                    "fps": fps,
                    "rotation": rotation,
                    "nb_frames": int(stream.get("nb_frames") or 0),
                }
            )
        elif stream.get("codec_type") == "audio" and not data.get("audio_codec"):
            data.update(
                {
                    "audio_codec": stream.get("codec_name", ""),
                    "audio_channels": int(stream.get("channels") or 0),
                    "audio_sample_rate": int(stream.get("sample_rate") or 0),
                    "has_audio": True,
                }
            )
    if not data["duration"]:
        data["duration"] = float(data.get("duration") or 0.0)
    width, height = _apply_rotation(int(data.get("width") or 0), int(data.get("height") or 0), int(data.get("rotation") or 0))
    data["width"], data["height"] = width, height
    return normalize_probe(data)


def _probe_with_ffmpeg(path: Path) -> dict[str, Any]:
    binary = ffmpeg_bin()
    proc = subprocess.run(
        [binary, "-hide_banner", "-nostdin", "-i", str(path)],
        capture_output=True,
        text=True,
        timeout=settings.media_probe_timeout_seconds,
    )
    stderr = proc.stderr
    data: dict[str, Any] = {}
    duration_match = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", stderr)
    if duration_match:
        hours, minutes, seconds = duration_match.groups()
        data["duration"] = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    video_match = re.search(r"Video: ([\w\d_]+).*?, (\d{2,5})x(\d{2,5})", stderr)
    if video_match:
        data["video_codec"], data["width"], data["height"] = video_match.group(1), int(video_match.group(2)), int(video_match.group(3))
    fps_match = re.search(r"(\d+(?:\.\d+)?) fps", stderr)
    if fps_match:
        data["fps"] = float(fps_match.group(1))
    audio_match = re.search(r"Audio: ([\w\d_]+).*?, (\d+) Hz, (\w+)", stderr)
    if audio_match:
        data["audio_codec"] = audio_match.group(1)
        data["audio_sample_rate"] = int(audio_match.group(2))
        channels = {"mono": 1, "stereo": 2, "5.1": 6, "7.1": 8}.get(audio_match.group(3), 2)
        data["audio_channels"] = channels
        data["has_audio"] = True
    if not data.get("duration"):
        raise MediaError("Unable to determine media duration.", code="media_unreadable")
    return normalize_probe(data)


# ------------------------------------------------------------------ helpers ---
def extract_audio(
    source: Path,
    target: Path,
    *,
    sample_rate: int = 16000,
    channels: int = 1,
    job_id: str | None = None,
    progress_cb: Callable[[float], None] | None = None,
    total_duration: float | None = None,
) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(
        [
            "-y",
            "-i",
            str(source),
            "-vn",
            "-ac",
            intnum(channels, minimum=1, maximum=8),
            "-ar",
            intnum(sample_rate, minimum=8000, maximum=48000),
            "-acodec",
            "pcm_s16le",
            "-progress",
            "pipe:1",
            str(target),
        ],
        job_id=job_id,
        total_duration=total_duration,
        progress_cb=progress_cb,
    )
    if not target.exists() or target.stat().st_size == 0:
        raise MediaError("No audio track could be extracted from this video.", code="no_audio")
    return target


def decode_pcm(
    source: Path,
    *,
    sample_rate: int = 8000,
    channels: int = 1,
    start: float | None = None,
    duration: float | None = None,
    job_id: str | None = None,
):
    """Stream decoded PCM into numpy as float32 in [-1, 1] (mono mixdown)."""
    import numpy as np

    args = ["-v", "error"]
    if start is not None:
        args += ["-ss", num(start, minimum=0, name="start")]
    if duration is not None:
        args += ["-t", num(duration, minimum=0.01, name="duration")]
    args += [
        "-i",
        str(source),
        "-vn",
        "-ac",
        intnum(channels, minimum=1, maximum=2),
        "-ar",
        intnum(sample_rate, minimum=1000, maximum=48000),
        "-f",
        "s16le",
        "-acodec",
        "pcm_s16le",
        "-",
    ]
    cmd = [ffmpeg_bin(), "-hide_banner", "-nostdin", *args]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
    if job_id:
        registry.register_process(job_id, proc)
    chunks: list[bytes] = []
    try:
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read(1 << 20)
            if not chunk:
                break
            chunks.append(chunk)
        if job_id and registry.is_cancelled(job_id):
            raise CancelledError_()
    finally:
        if proc.poll() is None:
            proc.terminate()
        proc.wait(timeout=30)
        if job_id:
            registry.unregister_process(job_id, proc)
    raw = b"".join(chunks)
    if not raw:
        return np.zeros(0, dtype="float32")
    samples = np.frombuffer(raw, dtype="<i2")
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1).astype("<i2")
    return samples.astype("float32") / 32768.0


def energy_envelope(
    source: Path,
    *,
    hop_seconds: float = 0.02,
    sample_rate: int = 8000,
    start: float | None = None,
    duration: float | None = None,
    job_id: str | None = None,
) -> dict[str, list[float]]:
    """Frame-level RMS + zero-crossing-rate envelope used for real acoustic
    features (energy, pause detection, speech rate, emphasis)."""
    import numpy as np

    samples = decode_pcm(
        source, sample_rate=sample_rate, channels=1, start=start, duration=duration, job_id=job_id
    )
    if samples.size == 0:
        return {"times": [], "rms": [], "zcr": []}
    hop = max(1, int(hop_seconds * sample_rate))
    frame_count = max(1, samples.size // hop)
    trimmed = samples[: frame_count * hop].reshape(frame_count, hop)
    rms = np.sqrt(np.mean(np.square(trimmed), axis=1))
    zcr = np.mean(np.abs(np.diff(np.sign(trimmed), axis=1)) > 0, axis=1)
    times = (np.arange(frame_count) * hop_seconds)
    return {
        "times": [round(float(t), 3) for t in times],
        "rms": [round(float(v), 5) for v in rms],
        "zcr": [round(float(v), 5) for v in zcr],
    }


def sample_frames(
    source: Path,
    target_dir: Path,
    *,
    fps: float = 2.0,
    width: int = 480,
    start: float | None = None,
    duration: float | None = None,
    job_id: str | None = None,
    progress_cb: Callable[[float], None] | None = None,
) -> list[Path]:
    """Extract evenly spaced JPEG frames for vision analysis."""
    target_dir.mkdir(parents=True, exist_ok=True)
    for existing in target_dir.glob("*.jpg"):
        existing.unlink(missing_ok=True)
    args = ["-y"]
    if start is not None:
        args += ["-ss", num(start, minimum=0, name="start")]
    if duration is not None:
        args += ["-t", num(duration, minimum=0.01, name="duration")]
    args += [
        "-i",
        str(source),
        "-vf",
        f"fps={num(fps, minimum=0.05, maximum=10, name='fps')},scale={intnum(width, minimum=64, maximum=1920)}:-2",
        "-q:v",
        "4",
        "-progress",
        "pipe:1",
        str(target_dir / "frame_%06d.jpg"),
    ]
    run_ffmpeg(args, job_id=job_id, progress_cb=progress_cb, total_duration=duration)
    return sorted(target_dir.glob("frame_*.jpg"))


def extract_thumbnail(
    source: Path, target: Path, *, at: float = 0.0, width: int = 640, job_id: str | None = None
) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(
        [
            "-y",
            "-ss",
            num(at, minimum=0, name="at"),
            "-i",
            str(source),
            "-frames:v",
            "1",
            "-vf",
            f"scale={intnum(width, minimum=64, maximum=2160)}:-2",
            "-q:v",
            "3",
            str(target),
        ],
        job_id=job_id,
    )
    return target


def stream_segment_args(source: Path, start: float, duration: float) -> list[str]:
    """Input args for cutting a segment with accurate seeking."""
    return [
        "-ss",
        num(start, minimum=0, name="start"),
        "-i",
        str(source),
        "-t",
        num(duration, minimum=0.05, name="duration"),
    ]


def media_duration(path: Path) -> float:
    try:
        return float(probe_media(path).get("duration") or 0.0)
    except MediaError:
        return 0.0


def iter_files(directory: Path, pattern: str = "*") -> Iterator[Path]:
    if not directory.exists():
        return
    for path in sorted(directory.glob(pattern)):
        if path.is_file():
            yield path


def cleanup_dir(directory: Path) -> None:
    if directory.exists():
        shutil.rmtree(directory, ignore_errors=True)


def ensure_dirs(*paths: Path) -> None:
    for path in paths:
        Path(path).mkdir(parents=True, exist_ok=True)


def font_file_candidates(directory: str | None = None) -> list[str]:
    """Font families discoverable on this machine (for the caption editor UI)."""
    root = Path(directory or settings.font_directory)
    found: set[str] = set()
    if root.exists():
        for path in root.rglob("*.ttf"):
            name = path.stem
            if name in {"DejaVuSans-Bold", "DejaVuSans", "DejaVuSans-Oblique"}:
                found.add("DejaVu Sans")
            elif name.lower().startswith("liberation"):
                found.add("Liberation Sans")
            elif name.lower().startswith("noto"):
                found.add("Noto Sans")
            else:
                found.add(name.replace("-", " ").strip())
        for path in root.rglob("*.otf"):
            found.add(path.stem.replace("-", " ").strip())
    return sorted(found) or ["DejaVu Sans"]


def font_available(family: str) -> bool:
    families = {f.lower() for f in font_file_candidates()}
    if family.lower() in families:
        return True
    # libass falls back to fontconfig; unknown families usually still resolve,
    # but we report what we can verify and let the UI show the discovered list.
    return any(family.lower().split()[0] in f for f in families)


def env_snapshot() -> dict[str, Any]:
    return {
        "ffmpeg": ffmpeg_bin(),
        "ffprobe": ffprobe_bin(),
        "os": os.name,
    }
