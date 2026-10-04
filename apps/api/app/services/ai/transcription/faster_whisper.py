"""Local transcription with faster-whisper (CTranslate2 Whisper).

This provider is genuinely local: the model runs on this machine and audio never
leaves it. It reports itself unavailable when the package or model weights are
missing, so the registry can fall back to a configured API provider instead of
pretending transcription worked.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable

from app.core.config import settings
from app.core.errors import ProviderError, ProviderUnavailableError
from app.core.logging import get_logger
from app.services.ai.types import ProviderInfo, SegmentTiming, TranscriptionResult, WordTiming

log = get_logger(__name__)

_model_lock = threading.Lock()
_models: dict[str, Any] = {}


def package_available() -> bool:
    try:
        import faster_whisper  # noqa: F401

        return True
    except Exception:
        return False


def model_is_cached(model: str, download_root: str | None = None) -> bool:
    """Check whether the CT2 weights already exist locally (no network needed)."""
    roots: list[Path] = []
    if download_root:
        roots.append(Path(download_root))
    roots.extend(
        [
            Path.home() / ".cache" / "huggingface" / "hub",
            Path.home() / ".cache" / "clipforge" / "whisper",
        ]
    )
    needle = model.replace("/", "--")
    for root in roots:
        if not root.exists():
            continue
        try:
            for entry in root.iterdir():
                name = entry.name.lower()
                if needle.lower() in name:
                    for weight in entry.rglob("model.bin"):
                        if weight.stat().st_size > 1024:
                            return True
        except OSError:
            continue
    return False


def availability() -> tuple[bool, str]:
    if not package_available():
        return False, "faster-whisper is not installed in this environment"
    if settings.whisper_local_files_only and not model_is_cached(settings.whisper_model, settings.whisper_download_root):
        return False, f"model '{settings.whisper_model}' is not present locally"
    return True, ""


def _load_model(model_name: str, device: str, compute_type: str):
    key = f"{model_name}|{device}|{compute_type}"
    with _model_lock:
        if key not in _models:
            from faster_whisper import WhisperModel

            kwargs: dict[str, Any] = {"device": device, "compute_type": compute_type}
            if settings.whisper_download_root:
                kwargs["download_root"] = settings.whisper_download_root
            if settings.whisper_local_files_only:
                kwargs["local_files_only"] = True
            log.info("whisper.loading", model=model_name, device=device, compute_type=compute_type)
            started = time.time()
            _models[key] = WhisperModel(model_name, **kwargs)
            log.info("whisper.loaded", model=model_name, seconds=round(time.time() - started, 1))
        return _models[key]


def release_models() -> None:
    with _model_lock:
        _models.clear()


class FasterWhisperProvider:
    name = "faster-whisper"
    is_local = True

    def __init__(self, model: str | None = None, device: str | None = None, compute_type: str | None = None) -> None:
        self.model = model or settings.whisper_model
        self.device = _resolve_device(device or settings.whisper_device)
        self.compute_type = compute_type or settings.whisper_compute_type

    def info(self) -> ProviderInfo:
        ok, detail = availability()
        return ProviderInfo(
            name=self.name,
            kind="transcription",
            available=ok,
            model=self.model,
            detail=detail,
            is_local=True,
        )

    def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None = None,
        progress_cb: Callable[[float, str], None] | None = None,
        duration_hint: float | None = None,
    ) -> TranscriptionResult:
        ok, detail = availability()
        if not ok:
            raise ProviderUnavailableError(
                "transcription",
                f"Local Whisper is unavailable ({detail}). Install faster-whisper and pre-download the "
                f"'{self.model}' model, or configure an API transcription provider.",
            )
        model = _load_model(self.model, self.device, self.compute_type)
        if progress_cb:
            progress_cb(5.0, "Loading speech model")
        try:
            segments_iter, info = model.transcribe(
                str(audio_path),
                language=language or None,
                beam_size=settings.whisper_beam_size,
                word_timestamps=True,
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 350},
                condition_on_previous_text=False,
            )
        except Exception as exc:
            raise ProviderError(self.name, f"model error: {exc}") from exc

        total = float(getattr(info, "duration", 0.0) or duration_hint or 0.0)
        segments: list[SegmentTiming] = []
        for segment in segments_iter:
            words: list[WordTiming] = []
            for word in getattr(segment, "words", None) or []:
                probability = float(getattr(word, "probability", 0.0) or 0.0)
                words.append(
                    WordTiming(
                        word=(word.word or "").strip(),
                        start=float(word.start or segment.start or 0.0),
                        end=float(word.end or segment.end or 0.0),
                        confidence=probability,
                    )
                )
            segments.append(
                SegmentTiming(
                    start=float(segment.start or 0.0),
                    end=float(segment.end or 0.0),
                    text=(segment.text or "").strip(),
                    words=words,
                    confidence=_avg([w.confidence for w in words]) if words else 0.0,
                )
            )
            if progress_cb and total:
                progress_cb(min(95.0, 5.0 + (segments[-1].end / total) * 90.0), "Transcribing audio")

        if not segments:
            raise ProviderError(self.name, "no speech recognised", retryable=False)

        return TranscriptionResult(
            segments=segments,
            language=getattr(info, "language", "") or "",
            language_probability=float(getattr(info, "language_probability", 0.0) or 0.0),
            duration=total or segments[-1].end,
            provider=self.name,
            model=self.model,
            has_word_timings=any(segment.words for segment in segments),
            meta={"device": self.device, "compute_type": self.compute_type},
        )


def _resolve_device(requested: str) -> str:
    if requested and requested != "auto":
        return requested
    try:
        import ctranslate2

        return "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
    except Exception:
        return "cpu"


def _avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0
