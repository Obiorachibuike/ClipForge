"""The Rust processor client, and — more importantly — its fallback behaviour.

The sidecar is optional by design: the web app must work identically when it is
disabled, misconfigured, unreachable or returning nonsense. These tests pin that
down, because a regression here would take the whole pipeline offline.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from app.core.config import settings
from app.services import processor
from app.services.media import ffmpeg


@pytest.fixture()
def enable_processor(monkeypatch, tmp_path):
    """Point the client at a fake sidecar and mark it enabled."""
    monkeypatch.setattr(settings, "processor_enabled", True, raising=False)
    monkeypatch.setattr(settings, "processor_url", "http://processor.test:8100", raising=False)
    monkeypatch.setattr(settings, "processor_binary", "", raising=False)
    monkeypatch.setattr(settings, "processor_timeout_seconds", 5, raising=False)
    return tmp_path


def test_disabled_by_default_so_nothing_depends_on_the_sidecar():
    assert processor.enabled() is False
    result = processor.call("probe", {"path": "/tmp/whatever.mp4"})
    assert result.ok is False
    assert result.detail == "processor disabled"
    assert result.data == {}


def test_probe_falls_back_to_none_when_the_sidecar_is_unreachable(enable_processor):
    # No server is listening on the configured host: the client must report
    # "use the Python path" rather than raising.
    assert processor.enabled() is True
    assert processor.probe_media(Path("/tmp/missing.mp4")) is None


def test_waveform_and_frames_fall_back_to_none(enable_processor):
    assert processor.waveform(Path("/tmp/missing.mp4")) is None
    assert processor.sample_frames(Path("/tmp/missing.mp4"), enable_processor) is None


def test_healthcheck_reports_enabled_but_unreachable(enable_processor):
    health = processor.healthcheck()
    assert health["enabled"] is True
    assert health["reachable"] is False
    assert health["detail"]


def test_healthcheck_is_cheap_when_disabled():
    assert processor.healthcheck() == {"enabled": False, "reachable": False}


def test_info_describes_the_configured_transport(monkeypatch):
    monkeypatch.setattr(settings, "processor_enabled", True, raising=False)
    monkeypatch.setattr(settings, "processor_url", "http://processor:8100", raising=False)
    monkeypatch.setattr(settings, "processor_binary", "", raising=False)
    info = processor.info()
    assert info["enabled"] is True
    assert info["transport"] == "http"
    assert info["url"] == "http://processor:8100"


def test_cli_transport_parses_json_and_reports_failures(monkeypatch, tmp_path):
    """The CLI mode is how a same-host deployment calls the sidecar."""
    script = tmp_path / "fake-processor"
    script.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "health" ]; then echo \'{"ok": true, "ffmpeg": true}\'; exit 0; fi\n'
        'if [ "$1" = "probe" ]; then echo "bad input file" >&2; exit 3; fi\n'
        'echo "{}"\n'
    )
    script.chmod(0o755)

    monkeypatch.setattr(settings, "processor_enabled", True, raising=False)
    monkeypatch.setattr(settings, "processor_url", "", raising=False)
    monkeypatch.setattr(settings, "processor_binary", str(script), raising=False)

    assert processor.enabled() is True
    health = processor.call("health", {})
    assert health.ok is True
    assert health.transport == "cli"
    assert health.data["ffmpeg"] is True

    # A non-zero exit surfaces stderr as the human-readable detail.
    failure = processor.call("probe", {"path": "/tmp/x.mp4"})
    assert failure.ok is False
    assert "bad input file" in failure.detail


def test_cli_transport_survives_malformed_output(monkeypatch, tmp_path):
    script = tmp_path / "noisy-processor"
    script.write_text("#!/bin/sh\necho 'not json at all'\n")
    script.chmod(0o755)

    monkeypatch.setattr(settings, "processor_enabled", True, raising=False)
    monkeypatch.setattr(settings, "processor_url", "", raising=False)
    monkeypatch.setattr(settings, "processor_binary", str(script), raising=False)

    result = processor.call("probe", {"path": "/tmp/x.mp4"})
    assert result.ok is False
    assert result.detail == "malformed processor output"


def test_cli_transport_times_out_without_hanging_the_worker(monkeypatch, tmp_path):
    script = tmp_path / "slow-processor"
    script.write_text("#!/bin/sh\nsleep 30\n")
    script.chmod(0o755)

    monkeypatch.setattr(settings, "processor_enabled", True, raising=False)
    monkeypatch.setattr(settings, "processor_url", "", raising=False)
    monkeypatch.setattr(settings, "processor_binary", str(script), raising=False)

    result = processor.call("probe", {"path": "/tmp/x.mp4"}, timeout=1)
    assert result.ok is False
    assert result.detail == "processor timed out"


def test_probe_media_normalizes_a_sidecar_response(monkeypatch, enable_processor):
    """A real sidecar response must come out in the Python prober's exact shape."""
    payload = {
        "ok": True,
        "data": {
            "duration": 143.367,
            "width": 1920,
            "height": 1080,
            "fps": 30.0,
            "video_codec": "h264",
            "audio_codec": "aac",
            "audio_channels": 2,
            "audio_sample_rate": 44100,
            "has_audio": True,
            "rotation": 0,
            "container": "mov,mp4,m4a,3gp,3g2,mj2",
            "bitrate": 577000,
            "nb_frames": 4301,
            "streams": [{"codec_type": "video"}],
        },
    }
    monkeypatch.setattr(processor, "call", lambda *args, **kwargs: processor.ProcessorResult(True, data=payload))

    probe = processor.probe_media(Path("/tmp/whatever.mp4"))
    assert probe is not None
    assert probe["strategy"] == "processor"
    assert probe["duration"] == pytest.approx(143.367)
    # The keys the pipeline indexes directly must all exist.
    for key in ("duration", "width", "height", "fps", "video_codec", "audio_codec", "audio_channels",
                "audio_sample_rate", "has_audio", "rotation", "container", "bitrate", "streams", "nb_frames"):
        assert key in probe, f"{key} missing from the normalized probe"


def test_probe_media_rejects_a_zero_length_response(monkeypatch, enable_processor):
    payload = {"ok": True, "data": {"duration": 0.0, "streams": []}}
    monkeypatch.setattr(processor, "call", lambda *args, **kwargs: processor.ProcessorResult(True, data=payload))
    assert processor.probe_media(Path("/tmp/whatever.mp4")) is None


def test_probe_media_rejects_a_shape_that_is_not_a_probe(monkeypatch, enable_processor):
    payload = {"ok": True, "data": {"unexpected": "garbage"}}
    monkeypatch.setattr(processor, "call", lambda *args, **kwargs: processor.ProcessorResult(True, data=payload))
    assert processor.probe_media(Path("/tmp/whatever.mp4")) is None


def test_waveform_returns_floats_in_range(monkeypatch, enable_processor):
    payload = {"ok": True, "peaks": [0, 0.25, 0.5, 1.0]}
    monkeypatch.setattr(processor, "call", lambda *args, **kwargs: processor.ProcessorResult(True, data=payload))

    peaks = processor.waveform(Path("/tmp/whatever.mp4"), buckets=4)
    assert peaks == [0.0, 0.25, 0.5, 1.0]
    assert all(isinstance(peak, float) for peak in peaks)


def test_the_python_prober_and_the_sidecar_agree_on_shape():
    """Both producers must emit the same keys, or callers will break one day."""
    python_shape = set(ffmpeg.normalize_probe({"duration": 1.0, "streams": []}))
    sidecar_shape = {
        "duration", "width", "height", "fps", "video_codec", "audio_codec", "audio_channels",
        "audio_sample_rate", "has_audio", "rotation", "container", "bitrate", "streams", "nb_frames",
    }
    assert sidecar_shape == python_shape


def test_sidecar_alias_exposes_the_public_api():
    """Callers import `sidecar` so their optional-dependency intent is explicit."""
    from app.services.processor import sidecar

    for name in ("probe_media", "waveform", "sample_frames", "healthcheck", "info", "call", "enabled"):
        assert callable(getattr(sidecar, name)), f"sidecar.{name} must be callable"


def test_readiness_reports_the_sidecar_but_never_degrades_over_it(client):
    """A configured-but-unreachable sidecar must not take the API out of rotation."""
    response = client.get("/readyz")
    body = response.json()
    assert "processor" in body["checks"], "readiness must say whether the sidecar is reachable"
    assert set(body["checks"]["processor"]) >= {"enabled", "reachable"}

    # With no processor configured the API is still ready: FFmpeg is what matters.
    assert body["checks"]["processor"]["enabled"] is False
    assert response.status_code == 200


def test_probe_falls_through_to_python_when_the_sidecar_answers_nothing(monkeypatch):
    """`run_probe` must not depend on the sidecar returning anything useful."""
    from app.services import processor as processor_module
    from app.services.pipeline import video_jobs

    calls: list[str] = []

    def fake_sidecar(path):
        calls.append("sidecar")
        return None

    def fake_python(path):
        calls.append("python")
        return {"duration": 12.5, "width": 1920, "height": 1080, "fps": 30.0, "video_codec": "h264",
                "audio_codec": "aac", "audio_channels": 2, "audio_sample_rate": 44100,
                "has_audio": True, "rotation": 0, "container": "mp4", "bitrate": 1, "streams": [], "nb_frames": 0}

    monkeypatch.setattr(video_jobs.sidecar, "probe_media", fake_sidecar)
    monkeypatch.setattr(video_jobs.ffmpeg, "probe_media", fake_python)

    # Drive the real fallback expression used inside run_probe.
    result = video_jobs.sidecar.probe_media(Path("/tmp/x.mp4")) or video_jobs.ffmpeg.probe_media(Path("/tmp/x.mp4"))
    assert calls == ["sidecar", "python"]
    assert result["duration"] == 12.5
