"""Build the FFmpeg render plan for a clip.

This module is the single place where an editable clip becomes a media graph,
which keeps rendering reproducible, testable and safe: every value is validated
before it becomes an argument, and captions/headlines are generated as files we
own rather than injected as filter strings from user input.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Sequence

from app.core.config import settings
from app.core.errors import MediaError
from app.services.media.ass import CaptionConfig, CaptionWord, HeadlineSpec, build_ass_document
from app.services.media.ffmpeg import choice, escape_expression, escape_filter_path, intnum, num
from app.services.media.framing import ASPECTS, crop_expression, static_crop_for

# Platform presets: every one of these is a real, rendered output specification.
RENDER_PRESETS: dict[str, dict[str, Any]] = {
    "tiktok": {"label": "TikTok", "width": 1080, "height": 1920, "aspect_ratio": "9:16", "fps": 30, "bitrate": "10M", "max_seconds": 600},
    "youtube_shorts": {"label": "YouTube Shorts", "width": 1080, "height": 1920, "aspect_ratio": "9:16", "fps": 30, "bitrate": "10M", "max_seconds": 180},
    "instagram_reels": {"label": "Instagram Reels", "width": 1080, "height": 1920, "aspect_ratio": "9:16", "fps": 30, "bitrate": "9M", "max_seconds": 180},
    "facebook_reels": {"label": "Facebook Reels", "width": 1080, "height": 1920, "aspect_ratio": "9:16", "fps": 30, "bitrate": "8M", "max_seconds": 180},
    "generic_vertical": {"label": "Generic Vertical", "width": 1080, "height": 1920, "aspect_ratio": "9:16", "fps": 30, "bitrate": "8M", "max_seconds": 600},
    "square": {"label": "Square", "width": 1080, "height": 1080, "aspect_ratio": "1:1", "fps": 30, "bitrate": "7M", "max_seconds": 600},
    "landscape": {"label": "Landscape (16:9)", "width": 1920, "height": 1080, "aspect_ratio": "16:9", "fps": 30, "bitrate": "8M", "max_seconds": 600},
}

ASPECT_DIMENSIONS: dict[str, tuple[int, int]] = {
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
    "16:9": (1920, 1080),
    "4:5": (1080, 1350),
    "3:4": (1080, 1440),
}

BACKGROUND_MODES = {"smart_fill", "blur", "solid"}
AUDIO_MODES = {"original", "muted", "mixed"}


@dataclass
class CaptionSpec:
    enabled: bool = True
    config: CaptionConfig = field(default_factory=CaptionConfig)
    words: list[CaptionWord] = field(default_factory=list)
    headline: HeadlineSpec | None = None
    style_name: str = ""


@dataclass
class RenderPlan:
    """A fully resolved, validated render description."""

    clip_id: str
    video_id: str
    preset: str
    aspect_ratio: str
    width: int
    height: int
    fps: int
    start: float
    duration: float
    source_duration: float
    video_codec: str
    audio_codec: str
    crf: int
    bitrate: str
    audio_bitrate: str
    preset_speed: str
    background_mode: str
    background_color: str
    blur_sigma: float
    crop_width: int
    crop_height: int
    crop_x: str
    crop_y: str
    crop_mode: str
    focus_x: float
    audio_mode: str
    audio_gain_db: float
    audio_normalize: bool
    fade_in: float
    fade_out: float
    music_key: str
    music_gain_db: float
    captions: CaptionSpec
    container: str = "mp4"
    warnings: list[str] = field(default_factory=list)
    framing_strategy: str = ""
    source_width: int = 0
    source_height: int = 0

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["captions"] = {
            "enabled": self.captions.enabled,
            "style_name": self.captions.style_name,
            "config": self.captions.config.to_dict(),
            "word_count": len(self.captions.words),
            "headline": self.captions.headline.text if self.captions.headline else "",
        }
        return data


def resolve_dimensions(preset: str, aspect_ratio: str | None) -> tuple[int, int, str]:
    key = str(preset or "generic_vertical").lower()
    if key in RENDER_PRESETS:
        entry = RENDER_PRESETS[key]
        return entry["width"], entry["height"], entry["aspect_ratio"]
    ratio = str(aspect_ratio or settings.default_aspect_ratio)
    if ratio not in ASPECT_DIMENSIONS:
        ratio = "9:16"
    width, height = ASPECT_DIMENSIONS[ratio]
    return width, height, ratio


def build_plan(
    *,
    clip_id: str,
    video_id: str,
    start: float,
    end: float,
    source_duration: float,
    source_width: int,
    source_height: int,
    preset: str = "generic_vertical",
    aspect_ratio: str | None = None,
    fps: int | None = None,
    crop: dict[str, Any] | None = None,
    background: dict[str, Any] | None = None,
    audio: dict[str, Any] | None = None,
    caption_config: dict[str, Any] | None = None,
    caption_style_name: str = "",
    words: Sequence[CaptionWord] | None = None,
    headline: dict[str, Any] | None = None,
    captions_enabled: bool = True,
    framing_keyframes: Sequence[dict[str, Any]] | None = None,
) -> RenderPlan:
    """Validate and assemble the render plan from clip edit state."""
    warnings: list[str] = []

    if end <= start:
        raise MediaError("The clip end must come after its start.", code="invalid_clip_range")
    if source_duration > 0 and start >= source_duration:
        raise MediaError("The clip starts beyond the end of the source video.", code="invalid_clip_range")

    duration = end - start
    if source_duration > 0:
        duration = min(duration, max(0.5, source_duration - start))

    width, height, ratio = resolve_dimensions(preset, aspect_ratio)
    target_fps = int(fps or RENDER_PRESETS.get(preset, {}).get("fps") or 30)
    target_fps = int(max(24, min(60, target_fps)))
    bitrate = str(RENDER_PRESETS.get(preset, {}).get("bitrate") or settings.render_default_bitrate)

    crop_config = dict(crop or {})
    background_config = dict(background or {})
    audio_config = dict(audio or {})

    # ------------------------------------------------------------ framing ---
    mode = str(crop_config.get("mode") or "auto")
    focus_x = max(0.0, min(1.0, float(crop_config.get("focus_x", 0.5) or 0.5)))
    strategy = str(crop_config.get("strategy") or "")
    keyframes = list(framing_keyframes or crop_config.get("keyframes") or [])
    background_mode = choice(background_config.get("mode"), BACKGROUND_MODES, default="smart_fill", name="background mode")
    background_color = _hex(background_config.get("color"), "#0B0B12")
    blur_sigma = _float_range(background_config.get("blur_sigma"), 6.0, 80.0, 28.0)

    source_width = int(source_width or 1920)
    source_height = int(source_height or 1080)

    if mode == "center":
        crop_width, crop_height, x, y = static_crop_for(ratio, source_width, source_height, 0.5)
        crop_x, crop_y = str(x), str(y)
    elif mode == "manual":
        crop_width, crop_height, x, y = static_crop_for(ratio, source_width, source_height, focus_x)
        crop_x, crop_y = str(x), str(y)
    elif mode in ("auto", "track") and background_mode == "smart_fill" and keyframes:
        crop_width, crop_height, x_expr, y_expr = crop_expression(
            keyframes, clip_start=start, source_width=source_width, source_height=source_height
        )
        crop_x, crop_y = x_expr, y_expr
        if strategy:
            warnings.append(f"framing:{strategy}")
    else:
        # Fit mode: the whole frame is kept and padded/blurred into the canvas.
        crop_width, crop_height = source_width, source_height
        crop_x, crop_y = "0", "0"
        if background_mode == "smart_fill":
            background_mode = "blur"
            warnings.append("background:switched_to_blur")

    if crop_width > source_width or crop_height > source_height:
        crop_width = min(crop_width, source_width)
        crop_height = min(crop_height, source_height)

    # -------------------------------------------------------------- audio ---
    audio_mode = choice(audio_config.get("mode"), AUDIO_MODES | {"original"}, default="original", name="audio mode")
    gain_db = _float_range(audio_config.get("gain_db"), -24.0, 24.0, 0.0)
    normalize = bool(audio_config.get("normalize", False))
    fade_in = _float_range(audio_config.get("fade_in"), 0.0, 10.0, 0.0)
    fade_out = _float_range(audio_config.get("fade_out"), 0.0, 10.0, 0.0)
    music_key = str(audio_config.get("music_key") or "")[:400]
    music_gain_db = _float_range(audio_config.get("music_gain_db"), -40.0, 6.0, -14.0)

    # ----------------------------------------------------------- captions ---
    config = CaptionConfig.from_dict(caption_config)
    headline_spec = None
    if headline and str(headline.get("text") or "").strip():
        headline_spec = HeadlineSpec(
            text=str(headline.get("text"))[:160],
            position="top" if str(headline.get("position", "top")) == "top" else "bottom",
            font_family=str(headline.get("font_family", "DejaVu Sans")),
            font_size=int(_float_range(headline.get("font_size"), 20, 140, 46)),
            bold=bool(headline.get("bold", True)),
            uppercase=bool(headline.get("uppercase", False)),
            text_color=_hex(headline.get("text_color"), "#FFFFFF"),
            background=choice(headline.get("background"), {"box", "outline"}, default="box", name="headline background"),
            background_color=_hex(headline.get("background_color"), "#0B0B12"),
            background_opacity=_float_range(headline.get("background_opacity"), 0.0, 1.0, 0.62),
            outline_color=_hex(headline.get("outline_color"), "#000000"),
            outline_width=int(_float_range(headline.get("outline_width"), 0, 10, 2)),
            margin_v=int(_float_range(headline.get("margin_v"), 0, 900, 140)),
            start=0.0,
            end=duration,
        )

    plan = RenderPlan(
        clip_id=clip_id,
        video_id=video_id,
        preset=preset,
        aspect_ratio=ratio,
        width=width,
        height=height,
        fps=target_fps,
        start=round(start, 3),
        duration=round(duration, 3),
        source_duration=round(source_duration, 3),
        video_codec=settings.render_video_codec,
        audio_codec=settings.render_audio_codec,
        crf=int(max(14, min(30, settings.render_crf))),
        bitrate=bitrate,
        audio_bitrate=settings.render_audio_bitrate,
        preset_speed=choice(settings.render_preset, {"ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow"}, default="medium"),
        background_mode=background_mode,
        background_color=background_color,
        blur_sigma=blur_sigma,
        crop_width=crop_width,
        crop_height=crop_height,
        crop_x=crop_x,
        crop_y=crop_y,
        crop_mode=mode,
        focus_x=focus_x,
        audio_mode=audio_mode,
        audio_gain_db=gain_db,
        audio_normalize=normalize,
        fade_in=fade_in,
        fade_out=fade_out,
        music_key=music_key,
        music_gain_db=music_gain_db,
        captions=CaptionSpec(
            enabled=bool(captions_enabled),
            config=config,
            words=list(words or []),
            headline=headline_spec,
            style_name=caption_style_name,
        ),
        warnings=warnings,
        framing_strategy=strategy,
        source_width=source_width,
        source_height=source_height,
    )
    return plan


def build_ass(
    plan: RenderPlan,
    *,
    document_start: float = 0.0,
    edited_words: dict[int, str] | None = None,
) -> str | None:
    """Produce the subtitle document for a plan, or None when there is nothing
    to burn in."""
    if not plan.captions.enabled:
        return None
    words = plan.captions.words
    if edited_words:
        for word in words:
            replacement = edited_words.get(word.index)
            if replacement is not None:
                word.text = replacement
                word.is_edited = True
    if not words and not plan.captions.headline:
        return None
    headline = None
    if plan.captions.headline:
        spec = plan.captions.headline
        spec.start = document_start
        spec.end = plan.duration - document_start
        headline = spec
    return build_ass_document(
        width=plan.width,
        height=plan.height,
        words=words,
        caption_config=plan.captions.config,
        headline=headline,
    )


def build_ffmpeg_args(
    plan: RenderPlan,
    *,
    source_path: str,
    target_path: str,
    ass_path: str | None = None,
    music_path: str | None = None,
) -> list[str]:
    """Assemble the full FFmpeg argument list for the plan.

    Video and audio chains live in a single `-filter_complex` graph so `-map`
    streams are unambiguous.
    """
    width, height = plan.width, plan.height
    graph: list[str] = []

    # Crop x/y may be either a number or a piecewise expression of `t`; the
    # expression form contains commas, which are structural inside a filtergraph.
    crop = (
        f"crop={intnum(plan.crop_width, minimum=2)}:{intnum(plan.crop_height, minimum=2)}"
        f":x={escape_expression(str(plan.crop_x))}:y={escape_expression(str(plan.crop_y))}"
    )
    scale = f"scale={width}:{height}:flags=lanczos"

    if plan.background_mode == "smart_fill":
        graph.append(f"[0:v]{crop},{scale},setsar=1[comp]")
    elif plan.background_mode == "blur":
        graph.append(
            f"[0:v]{crop},scale={width}:{height}:force_original_aspect_ratio=decrease:flags=lanczos,setsar=1[fg]"
        )
        graph.append(
            f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={width}:{height},boxblur={num(plan.blur_sigma, minimum=1, maximum=80)}:2,eq=brightness=-0.06[bg]"
        )
        graph.append("[bg][fg]overlay=x=(W-w)/2:y=(H-h)/2:format=auto[comp]")
    elif plan.background_mode == "solid":
        graph.append(
            f"[0:v]{crop},scale={width}:{height}:force_original_aspect_ratio=decrease:flags=lanczos,setsar=1[fg]"
        )
        graph.append(f"color=c={plan.background_color}:s={width}x{height}:d={num(plan.duration, minimum=0.1)}[bg]")
        graph.append("[bg][fg]overlay=x=(W-w)/2:y=(H-h)/2:format=auto[comp]")
    else:  # pragma: no cover - validated in build_plan
        raise MediaError("Unsupported background mode.", code="unsupported_background")

    video_chain: list[str] = []
    if plan.captions.enabled and ass_path:
        video_chain.append(f"ass={escape_filter_path(ass_path)}")
    video_chain.append(f"fps={intnum(plan.fps, minimum=1, maximum=60)}")
    video_chain.append("format=yuv420p")
    graph.append(f"[comp]{','.join(video_chain)}[vout]")

    audio_filters: list[str] = []
    if plan.audio_normalize:
        audio_filters.append("loudnorm=I=-16:TP=-1.5:LRA=11")
    if abs(plan.audio_gain_db) > 0.01:
        audio_filters.append(f"volume={num(plan.audio_gain_db, minimum=-24, maximum=24)}dB")
    if plan.fade_in > 0:
        audio_filters.append(f"afade=t=in:st=0:d={num(plan.fade_in, minimum=0.01, maximum=10)}")
    if plan.fade_out > 0:
        audio_filters.append(
            f"afade=t=out:st={num(max(0.0, plan.duration - plan.fade_out), minimum=0)}:"
            f"d={num(plan.fade_out, minimum=0.01, maximum=10)}"
        )

    args: list[str] = ["-y", "-ss", num(plan.start, minimum=0), "-i", source_path]
    map_audio = True
    if plan.audio_mode == "muted":
        map_audio = False
    elif plan.audio_mode == "mixed" and music_path:
        args += ["-stream_loop", "-1", "-i", music_path]
        graph.append(f"[0:a]{','.join(audio_filters) if audio_filters else 'anull'}[voice]")
        graph.append(f"[1:a]volume={num(plan.music_gain_db, minimum=-40, maximum=6)}dB,atrim=0:{num(plan.duration)}[music]")
        graph.append("[voice][music]amix=inputs=2:duration=first:dropout_transition=2[aout]")
    elif audio_filters:
        graph.append(f"[0:a]{','.join(audio_filters)}[aout]")

    args += ["-filter_complex", ";".join(graph), "-map", "[vout]"]
    if map_audio:
        args += ["-map", "[aout]"] if (audio_filters or (plan.audio_mode == "mixed" and music_path)) else ["-map", "0:a?"]

    args += [
        "-t",
        num(plan.duration, minimum=0.1),
        "-c:v",
        plan.video_codec,
        "-preset",
        plan.preset_speed,
        "-crf",
        intnum(plan.crf, minimum=14, maximum=30),
        "-maxrate",
        plan.bitrate,
        "-bufsize",
        _double_bitrate(plan.bitrate),
        "-pix_fmt",
        "yuv420p",
        "-profile:v",
        "high",
        "-movflags",
        "+faststart",
    ]
    if settings.render_threads:
        args += ["-threads", intnum(settings.render_threads, minimum=1, maximum=64)]
    args += ["-c:a", plan.audio_codec, "-b:a", plan.audio_bitrate, "-ar", "48000", "-ac", "2"]
    if not map_audio:
        args += ["-an"]
    args += ["-progress", "pipe:1", target_path]
    return args


def _double_bitrate(bitrate: str) -> str:
    try:
        value = float(str(bitrate).rstrip("MmKk"))
        suffix = "M" if "M" in str(bitrate).upper() else "K"
        return f"{(value * 2):g}{suffix}"
    except Exception:
        return "16M"


def _hex(value: Any, default: str) -> str:
    text = str(value or "").strip()
    if text.startswith("#"):
        text = text[1:]
    if len(text) in (3, 6) and all(ch in "0123456789abcdefABCDEF" for ch in text):
        return f"0x{text}" if len(text) == 6 else f"0x{''.join(ch * 2 for ch in text)}"
    return default


def _float_range(value: Any, minimum: float, maximum: float, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))
