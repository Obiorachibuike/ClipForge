"""Transcription through an OpenAI-compatible /audio/transcriptions endpoint.

Works with OpenAI, Groq, Azure-style gateways, self-hosted whisper.cpp servers
that expose the OpenAI schema, and any custom gateway.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from app.core.config import settings
from app.core.errors import ProviderError, ProviderUnavailableError
from app.core.logging import get_logger
from app.services.ai.http import request_json
from app.services.ai.types import ProviderInfo, SegmentTiming, TranscriptionResult, WordTiming

log = get_logger(__name__)

MAX_UPLOAD_BYTES = 24 * 1024 * 1024  # most gateways cap around 25 MB


class OpenAICompatibleTranscriptionProvider:
    name = "openai"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        label: str = "openai",
    ) -> None:
        self.api_key = (api_key if api_key is not None else settings.openai_api_key).strip()
        self.base_url = (base_url or settings.openai_base_url).rstrip("/")
        self.model = model or settings.openai_transcription_model
        self.name = label
        self.is_local = False

    def info(self) -> ProviderInfo:
        available = bool(self.api_key and self.base_url)
        return ProviderInfo(
            name=self.name,
            kind="transcription",
            available=available,
            model=self.model,
            detail="" if available else "no API key configured",
            requires_key=True,
            is_local=False,
        )

    def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None = None,
        progress_cb: Callable[[float, str], None] | None = None,
        duration_hint: float | None = None,
    ) -> TranscriptionResult:
        if not self.api_key:
            raise ProviderUnavailableError(
                "transcription",
                "Set OPENAI_API_KEY (or configure a provider in Settings) to use API transcription.",
            )
        path = Path(audio_path)
        size = path.stat().st_size
        if size > MAX_UPLOAD_BYTES:
            raise ProviderError(
                self.name,
                f"audio file is {size / 1e6:.1f} MB which exceeds the {MAX_UPLOAD_BYTES / 1e6:.0f} MB API limit; "
                "split the audio or use a local Whisper model",
                retryable=False,
            )
        if progress_cb:
            progress_cb(10.0, "Uploading audio for transcription")
        data = {
            "model": self.model,
            "response_format": "verbose_json",
            "timestamp_granularities[]": "word",
        }
        if language:
            data["language"] = language
        with open(path, "rb") as handle:
            payload = request_json(
                self.name,
                "POST",
                f"{self.base_url}/audio/transcriptions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                data=data,
                files={"file": (path.name, handle, "audio/wav")},
                timeout=max(settings.ai_request_timeout_seconds, 300),
            )
        if progress_cb:
            progress_cb(85.0, "Processing transcript")
        return self._parse(payload)

    def _parse(self, payload: dict) -> TranscriptionResult:
        segments_raw = payload.get("segments") or []
        words_raw = payload.get("words") or []
        segments: list[SegmentTiming] = []

        def to_word(entry: dict) -> WordTiming:
            return WordTiming(
                word=str(entry.get("word") or entry.get("text") or "").strip(),
                start=float(entry.get("start") or 0.0),
                end=float(entry.get("end") or 0.0),
                confidence=float(entry.get("confidence", entry.get("probability", 0.0)) or 0.0),
            )

        if segments_raw:
            for entry in segments_raw:
                word_entries = entry.get("words") or []
                segments.append(
                    SegmentTiming(
                        start=float(entry.get("start") or 0.0),
                        end=float(entry.get("end") or 0.0),
                        text=str(entry.get("text") or "").strip(),
                        words=[to_word(w) for w in word_entries],
                        confidence=float(entry.get("avg_logprob", 0.0) or 0.0),
                    )
                )
        elif words_raw:
            # word-only response: synthesise segments from sentence groups
            current: list[WordTiming] = []
            for entry in words_raw:
                current.append(to_word(entry))
                if current[-1].word.endswith((".", "!", "?")) or len(current) >= 14:
                    segments.append(
                        SegmentTiming(
                            start=current[0].start,
                            end=current[-1].end,
                            text=" ".join(w.word for w in current),
                            words=list(current),
                        )
                    )
                    current = []
            if current:
                segments.append(
                    SegmentTiming(start=current[0].start, end=current[-1].end, text=" ".join(w.word for w in current), words=list(current))
                )
        else:
            text = str(payload.get("text") or "").strip()
            if not text:
                raise ProviderError(self.name, "empty transcript returned", retryable=False)
            duration = float(payload.get("duration") or 0.0)
            segments = [
                SegmentTiming(
                    start=0.0,
                    end=duration,
                    text=text,
                    words=[],
                    confidence=0.0,
                )
            ]
            log.warning("transcription.no_word_timings", provider=self.name)

        # Attach orphan words to the segment they fall inside.
        all_segment_words = sum(len(s.words) for s in segments)
        return TranscriptionResult(
            segments=segments,
            language=str(payload.get("language") or ""),
            language_probability=0.0,
            duration=float(payload.get("duration") or (segments[-1].end if segments else 0.0)),
            provider=self.name,
            model=str(payload.get("model") or self.model),
            has_word_timings=all_segment_words > 0,
            meta={"raw_word_count": len(words_raw)},
        )


def endpoint_diagnostics() -> dict:
    """Used by the settings page to explain what is configured."""
    return {
        "base_url": settings.openai_base_url,
        "model": settings.openai_transcription_model,
        "has_key": bool(settings.openai_api_key),
        "compatible_with": ["openai", "azure-openai", "groq", "together", "whisper.cpp server", "custom gateway"],
        "note": json.dumps({"schema": "OpenAI verbose_json with word granularity"}),
    }
