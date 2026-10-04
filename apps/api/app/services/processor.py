"""Client for the optional Rust media processor.

The Rust service (``apps/processor/rust``) is an independently deployable,
horizontally scalable orchestration sidecar. It owns no state and never becomes
a requirement: every call here degrades to the in-process Python implementation
when the sidecar is not configured or not reachable.

Two transports are supported because they fit different deployments:

* HTTP (``PROCESSOR_URL``) - the sidecar runs as its own service.
* CLI  (``PROCESSOR_BINARY``) - the sidecar is on the same host, called per task.

Both paths speak the same JSON contract, and results are validated before use.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)


@dataclass
class ProcessorResult:
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    detail: str = ""
    duration_ms: float = 0.0
    transport: str = ""


class ProcessorUnavailable(RuntimeError):
    """Raised when the sidecar is requested explicitly but cannot be used."""


def binary_path() -> str:
    """Resolve the compiled sidecar binary (env override, then PATH)."""
    explicit = getattr(settings, "processor_binary", "") or ""
    if explicit and Path(explicit).exists():
        return explicit
    return shutil.which("clipforge-processor") or ""


def enabled() -> bool:
    return bool(settings.processor_enabled and (settings.processor_url or binary_path()))


def info() -> dict[str, Any]:
    """Describe the sidecar for /capabilities. Never probes the network here."""
    binary = binary_path()
    return {
        "enabled": enabled(),
        "configured": bool(settings.processor_url or binary),
        "binary": binary,
        "url": settings.processor_url,
        "transport": "http" if settings.processor_url else ("cli" if binary else "none"),
        "timeout_seconds": settings.processor_timeout_seconds,
    }


# --------------------------------------------------------------------- HTTP ---
def _http_request(path: str, payload: dict[str, Any], timeout: float | None = None) -> ProcessorResult:
    import urllib.error
    import urllib.request

    url = f"{settings.processor_url.rstrip('/')}{path}"
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers={"content-type": "application/json"})
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout or settings.processor_timeout_seconds) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:  # the sidecar reports errors as JSON
        detail = exc.read().decode("utf-8", "replace")[:400]
        return ProcessorResult(False, detail=f"http {exc.code}: {detail}", transport="http")
    except Exception as exc:
        return ProcessorResult(False, detail=f"{type(exc).__name__}: {exc}", transport="http")
    duration = (time.perf_counter() - started) * 1000
    try:
        data = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        return ProcessorResult(False, detail="malformed response", duration_ms=duration, transport="http")
    return ProcessorResult(bool(data.get("ok", True)), data=data, duration_ms=duration, transport="http")


# ---------------------------------------------------------------------- CLI ---
def _cli_request(command: str, payload: dict[str, Any], timeout: float | None = None) -> ProcessorResult:
    binary = binary_path()
    if not binary:
        return ProcessorResult(False, detail="processor binary not found", transport="cli")
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            [binary, command],
            input=json.dumps(payload).encode("utf-8"),
            capture_output=True,
            timeout=timeout or settings.processor_timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return ProcessorResult(False, detail="processor timed out", transport="cli")
    except Exception as exc:  # pragma: no cover - environment dependent
        return ProcessorResult(False, detail=f"{type(exc).__name__}: {exc}", transport="cli")
    duration = (time.perf_counter() - started) * 1000
    if completed.returncode != 0:
        return ProcessorResult(
            False,
            detail=completed.stderr.decode("utf-8", "replace")[:400] or f"exit {completed.returncode}",
            duration_ms=duration,
            transport="cli",
        )
    try:
        data = json.loads(completed.stdout.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return ProcessorResult(False, detail="malformed processor output", duration_ms=duration, transport="cli")
    return ProcessorResult(bool(data.get("ok", True)), data=data, duration_ms=duration, transport="cli")


def call(command: str, payload: dict[str, Any], *, timeout: float | None = None) -> ProcessorResult:
    """Invoke a sidecar command over whichever transport is configured."""
    if not enabled():
        return ProcessorResult(False, detail="processor disabled")
    if settings.processor_url:
        result = _http_request(f"/v1/{command}", payload, timeout=timeout)
        if result.ok:
            return result
        log.warning("processor.http_failed", command=command, detail=result.detail)
        if not binary_path():
            return result
    return _cli_request(command, payload, timeout=timeout)


def healthcheck() -> dict[str, Any]:
    """Used by /readyz to report sidecar reachability without failing the API."""
    if not enabled():
        return {"enabled": False, "reachable": False}
    result = call("health", {}, timeout=5)
    return {"enabled": True, "reachable": result.ok, "detail": result.detail, "transport": result.transport}


# ------------------------------------------------------------- capabilities ---
def probe_media(path: Path) -> dict[str, Any] | None:
    """Fast probe through the sidecar; ``None`` means "use the Python path"."""
    result = call("probe", {"path": str(path)})
    if not result.ok:
        return None
    streams = result.data.get("streams") or {}
    if not result.data.get("duration") and not streams:
        return None
    return result.data


def waveform(path: Path, buckets: int = 400) -> list[float] | None:
    """Peak envelope for the timeline UI. Real PCM scan, done concurrently."""
    result = call("waveform", {"path": str(path), "buckets": max(20, min(buckets, 4000))})
    peaks = result.data.get("peaks") if result.ok else None
    if not peaks:
        return None
    return [float(value) for value in peaks]


def sample_frames(
    path: Path,
    output_dir: Path,
    *,
    fps: float = 1.0,
    width: int = 320,
) -> list[dict[str, Any]] | None:
    """Extract thumbnail frames in parallel. Returns frame metadata or ``None``."""
    result = call(
        "frames",
        {
            "path": str(path),
            "output_dir": str(output_dir),
            "fps": float(fps),
            "width": int(width),
        },
        timeout=max(settings.processor_timeout_seconds, 600),
    )
    frames = result.data.get("frames") if result.ok else None
    if not frames:
        return None
    return list(frames)
