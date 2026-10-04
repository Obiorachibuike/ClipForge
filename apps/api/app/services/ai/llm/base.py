"""Shared language-model behaviour: prompts, JSON validation, narration.

Concrete providers only implement transport (`_chat`). Everything about *what*
we ask and *how* we validate answers lives here, so all providers behave
identically and no provider can inject invented timestamps into the pipeline:
we only ever accept copy (title/hook/reason) and bounded scores from a model.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any

from app.core.logging import get_logger
from app.services.ai.http import parse_json_loose
from app.services.ai.types import ClipNarration, LLMResult, MomentAnalysis

log = get_logger(__name__)

NARRATION_SYSTEM = (
    "You are a short-form video editor. You write tight, specific copy for vertical clips. "
    "You never invent facts, quotes or timestamps. You only describe what is in the transcript."
)

NARRATION_PROMPT = """Below are candidate moments cut from one long video transcript.
For each moment write copy that a creator could publish. Rules:
- title: max 60 characters, specific, no clickbait, no quotes
- hook: one sentence (max 120 chars) taken or lightly tightened from the transcript opening
- reason: one sentence explaining why this moment stands on its own
- headlines: 3 alternative headline options, each max 60 characters

Return ONLY JSON of this shape:
{{"clips":[{{"id":"<id>","title":"...","hook":"...","reason":"...","headlines":["...","...","..."]}}]}}

Moments:
{moments}
"""

SCORING_PROMPT = """Score this transcript moment for short-form video potential.
Use only the transcript text. Return ONLY JSON:
{{"engagement":0-100,"hook_strength":0-100,"standalone":0-100,"clarity":0-100,
"category":"insight|story|lesson|question|funny|emotional|controversial|explanation",
"reason":"one sentence"}}

Moment transcript:
\"\"\"{text}\"\"\"
"""

HEADLINE_PROMPT = """Write {count} short headline options (max 60 characters each) for a vertical clip.
Context: {context}
Return ONLY a JSON array of strings."""


class BaseLLMProvider(ABC):
    """Transport-agnostic LLM provider."""

    name: str = "llm"
    is_local: bool = False
    default_model: str = ""

    @abstractmethod
    def _chat(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        temperature: float,
        json_mode: bool,
    ) -> LLMResult:
        """Send chat messages, return normalised result."""

    # ----------------------------------------------------------- interface ---
    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 1200,
        temperature: float = 0.4,
        json_mode: bool = False,
    ) -> LLMResult:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return self._chat(messages, max_tokens=max_tokens, temperature=temperature, json_mode=json_mode)

    # ------------------------------------------------------------- prompts ---
    def narrate_clips(self, moments: list[dict], *, language: str = "en") -> list[ClipNarration]:
        if not moments:
            return []
        blocks: list[str] = []
        for moment in moments:
            excerpt = str(moment.get("transcript", ""))[:1400]
            blocks.append(
                json.dumps(
                    {
                        "id": moment.get("id", ""),
                        "duration_seconds": round(float(moment.get("duration", 0.0)), 1),
                        "transcript": excerpt,
                    },
                    ensure_ascii=False,
                )
            )
        prompt = NARRATION_PROMPT.format(moments="\n".join(blocks))
        result = self.complete(
            prompt,
            system=NARRATION_SYSTEM,
            max_tokens=min(3000, 400 + 320 * len(moments)),
            temperature=0.5,
            json_mode=True,
        )
        payload = parse_json_loose(result.text)
        narrations: list[ClipNarration] = []
        if not isinstance(payload, dict):
            log.warning("llm.narration_unparseable", provider=self.name, sample=result.text[:200])
            return narrations
        by_id = {str(item.get("id", "")): item for item in payload.get("clips", []) if isinstance(item, dict)}
        for moment in moments:
            item = by_id.get(str(moment.get("id", "")))
            if not item:
                narrations.append(ClipNarration())
                continue
            headlines = [str(h)[:80] for h in (item.get("headlines") or []) if str(h).strip()][:3]
            narrations.append(
                ClipNarration(
                    title=_clean(item.get("title"), 80),
                    hook=_clean(item.get("hook"), 200),
                    reason=_clean(item.get("reason"), 300),
                    headline_variants=headlines,
                )
            )
        return narrations

    def analyze_moment(self, moment: dict) -> MomentAnalysis:
        text = str(moment.get("transcript", ""))[:2000]
        if not text.strip():
            return MomentAnalysis()
        result = self.complete(SCORING_PROMPT.format(text=text), max_tokens=400, temperature=0.2, json_mode=True)
        payload = parse_json_loose(result.text)
        if not isinstance(payload, dict):
            return MomentAnalysis()
        return MomentAnalysis(
            engagement=_score(payload.get("engagement")),
            hook_strength=_score(payload.get("hook_strength")),
            standalone=_score(payload.get("standalone")),
            clarity=_score(payload.get("clarity")),
            category=_clean(payload.get("category"), 32) or "insight",
            reason=_clean(payload.get("reason"), 300),
        )

    def headline_variants(self, context: dict, *, count: int = 5) -> list[str]:
        prompt = HEADLINE_PROMPT.format(count=count, context=json.dumps(context, ensure_ascii=False)[:1200])
        result = self.complete(prompt, max_tokens=400, temperature=0.8, json_mode=False)
        payload = parse_json_loose(result.text)
        variants: list[str] = []
        if isinstance(payload, list):
            variants = [str(item)[:80] for item in payload if str(item).strip()]
        else:
            variants = [line.strip(" -•\t")[:80] for line in result.text.splitlines() if line.strip()][:count]
        return [v for v in variants if v][: max(1, count)]


def _clean(value: Any, max_length: int) -> str:
    return str(value or "").strip().replace("\n", " ")[:max_length]


def _score(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(100.0, number))
