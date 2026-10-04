"""Provider interfaces.

Business logic depends on these protocols only. Adding Paystack-style vendors
for AI (a new transcription or language-model backend) means implementing one
class and registering it - never touching the pipeline.
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from app.services.ai.types import (
    ClipNarration,
    LLMResult,
    MomentAnalysis,
    ProviderInfo,
    TranscriptionResult,
)


@runtime_checkable
class TranscriptionProvider(Protocol):
    """Turns audio into timed text."""

    name: str
    is_local: bool

    def info(self) -> ProviderInfo: ...

    def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None = None,
        progress_cb=None,  # Callable[[float, str], None] | None
    ) -> TranscriptionResult: ...


@runtime_checkable
class LanguageModelProvider(Protocol):
    """Narrates and evaluates candidate moments."""

    name: str
    is_local: bool

    def info(self) -> ProviderInfo: ...

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 1200,
        temperature: float = 0.4,
        json_mode: bool = False,
    ) -> LLMResult: ...

    def narrate_clips(self, moments: list[dict], *, language: str = "en") -> list[ClipNarration]: ...

    def analyze_moment(self, moment: dict) -> MomentAnalysis: ...

    def headline_variants(self, context: dict, *, count: int = 5) -> list[str]: ...


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Similarity scoring used by the discovery engine."""

    name: str
    dimensions: int

    def info(self) -> ProviderInfo: ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...

    def similarity(self, a: list[float], b: list[float]) -> float: ...
