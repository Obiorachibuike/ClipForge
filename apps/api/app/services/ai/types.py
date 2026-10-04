"""Provider-agnostic AI data types.

Every provider (local Whisper, OpenAI-compatible, Gemini, Anthropic, custom)
normalises its output into these structures, so the pipeline downstream never
knows which vendor produced the data.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class WordTiming:
    word: str
    start: float
    end: float
    confidence: float = 0.0
    speaker: str = ""


@dataclass
class SegmentTiming:
    start: float
    end: float
    text: str
    words: list[WordTiming] = field(default_factory=list)
    speaker: str = ""
    confidence: float = 0.0


@dataclass
class TranscriptionResult:
    segments: list[SegmentTiming]
    language: str = ""
    language_probability: float = 0.0
    duration: float = 0.0
    provider: str = ""
    model: str = ""
    has_word_timings: bool = False
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def words(self) -> list[WordTiming]:
        return [word for segment in self.segments for word in segment.words]

    @property
    def text(self) -> str:
        return " ".join(segment.text.strip() for segment in self.segments).strip()


@dataclass
class ProviderInfo:
    name: str
    kind: str
    available: bool
    model: str = ""
    detail: str = ""
    requires_key: bool = False
    is_local: bool = False


@dataclass
class ClipNarration:
    """LLM-authored copy for one candidate. Never invents timing."""

    title: str = ""
    hook: str = ""
    reason: str = ""
    headline_variants: list[str] = field(default_factory=list)


@dataclass
class LLMUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class LLMResult:
    text: str
    model: str = ""
    provider: str = ""
    usage: LLMUsage = field(default_factory=LLMUsage)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class MomentAnalysis:
    """LLM assessment of a candidate window (scores 0-100, JSON validated)."""

    engagement: float = 0.0
    hook_strength: float = 0.0
    standalone: float = 0.0
    clarity: float = 0.0
    category: str = "insight"
    reason: str = ""
    title: str = ""
    hook: str = ""
