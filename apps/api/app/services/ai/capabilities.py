"""Truthful capability reporting for AI + media subsystems.

The UI reads these to decide what to show. A capability is reported available
only when the deployment can actually execute it - never to make a screen look
complete. Re-exported separately from the registry so configuration code can
import capabilities without pulling provider implementations into module load.
"""
from __future__ import annotations

from typing import Any

from app.services.ai.registry import available_providers, vision_capability

__all__ = ["available_providers", "vision_capability", "media_capability"]


def media_capability(settings_obj: Any = None) -> dict[str, Any]:
    """What the media pipeline can do right now (FFmpeg, FFprobe, fonts, CV)."""
    from app.core.config import settings as default_settings

    settings_obj = settings_obj or default_settings
    ffmpeg = settings_obj.ffmpeg_path
    ffprobe = settings_obj.ffprobe_path

    fonts = []
    try:
        from app.services.media.ass import available_fonts

        fonts = available_fonts()
    except Exception:  # pragma: no cover - cosmetic only
        fonts = []

    return {
        "ffmpeg": bool(ffmpeg),
        "ffprobe": bool(ffprobe),
        "ffmpeg_path": ffmpeg or "",
        "hardware_encoders": _hardware_encoders(ffmpeg) if ffmpeg else [],
        "fonts": fonts,
        "ass_subtitles": bool(ffmpeg),
        "loudness_normalisation": bool(ffmpeg),
    }


def _hardware_encoders(ffmpeg_path: str) -> list[str]:
    """Report encoder availability by asking FFmpeg, not by guessing."""
    import subprocess

    try:
        output = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=20,
        ).stdout
    except Exception:  # pragma: no cover - environment dependent
        return []
    found = []
    for encoder in ("h264_nvenc", "hevc_nvenc", "h264_vaapi", "h264_qsv", "h264_videotoolbox"):
        if encoder in output:
            found.append(encoder)
    return found
