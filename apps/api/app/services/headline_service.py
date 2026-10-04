"""Headline generation.

Two honest modes:
  * LLM configured -> authored variations, provider recorded in the response.
  * No LLM        -> extractive suggestions derived from the transcript itself
                     (opening lines, questions, and keyword-focused phrases).
A headline is never placed on the video without the user choosing it.
"""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models import Clip, ClipCandidate, Transcript, TranscriptStatus, TranscriptWord, User
from app.services.ai import registry as ai_registry
from app.services.pipeline.analyzer import build_corpus_idf, build_sentences
from app.services.ai.types import WordTiming

log = get_logger(__name__)

MAX_HEADLINE_CHARS = 70


class HeadlineService:
    @staticmethod
    def clip_text(db: Session, clip: Clip) -> str:
        if clip.candidate_id:
            candidate = db.get(ClipCandidate, clip.candidate_id)
            if candidate and candidate.transcript_text:
                return candidate.transcript_text
        transcript = db.execute(
            select(Transcript)
            .where(Transcript.video_id == clip.video_id, Transcript.status == TranscriptStatus.COMPLETED.value)
            .order_by(Transcript.created_at.desc())
        ).scalars().first()
        if transcript is None:
            return clip.headline or clip.title
        rows = db.execute(
            select(TranscriptWord)
            .where(
                TranscriptWord.transcript_id == transcript.id,
                TranscriptWord.start_time >= clip.start_time,
                TranscriptWord.end_time <= clip.end_time,
            )
            .order_by(TranscriptWord.start_time)
        ).scalars()
        return " ".join(row.word for row in rows)

    @staticmethod
    def extractive_suggestions(text: str, *, count: int = 5, title: str = "") -> list[str]:
        """Suggestions taken verbatim (or lightly trimmed) from the transcript."""
        if not text.strip():
            return []
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
        if not sentences:
            return []
        words = [WordTiming(word=token, start=index * 0.5, end=index * 0.5 + 0.5) for index, token in enumerate(text.split())]
        sentences_model = build_sentences(words)
        idf = build_corpus_idf(sentences_model)
        keywords = sorted(idf.items(), key=lambda kv: -kv[1])[:8]

        suggestions: list[str] = []

        def add(candidate: str) -> None:
            cleaned = HeadlineService._tidy(candidate)
            if cleaned and cleaned.lower() not in {s.lower() for s in suggestions}:
                suggestions.append(cleaned)

        first = sentences[0]
        add(first)
        for sentence in sentences[1:4]:
            if "?" in sentence:
                add(sentence)
        # Question + answer pairing reads well as a hook.
        questions = [s for s in sentences if s.endswith("?")]
        statements = [s for s in sentences if not s.endswith("?") and len(s) > 20]
        if questions and statements:
            add(f"{questions[0]} {statements[0]}")
        # Keyword-focused variants, phrased from real transcript content.
        for term, _weight in keywords[:3]:
            matching = next((s for s in sentences if term.lower() in s.lower()), "")
            if matching:
                add(matching)
        for sentence in sentences:
            if any(marker in sentence.lower() for marker in ("mistake", "nobody", "most people", "the truth", "stop ", "why ")):
                add(sentence)
            if len(suggestions) >= count:
                break
        if title and len(suggestions) < count:
            add(title)
        return suggestions[:count]

    @staticmethod
    def _tidy(text: str) -> str:
        cleaned = re.sub(r"^(and|but|so|well|okay|ok|yeah|um|uh|you know|i mean)[,\s]+", "", text.strip(), flags=re.I)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" .,\"'")
        if not cleaned:
            return ""
        if len(cleaned) > MAX_HEADLINE_CHARS:
            cleaned = cleaned[: MAX_HEADLINE_CHARS - 1].rstrip() + "…"
        return cleaned[0].upper() + cleaned[1:] if cleaned else ""

    @staticmethod
    def generate(
        db: Session,
        user: User,
        clip: Clip,
        *,
        count: int = 5,
        tone: str = "default",
        refresh: bool = False,
    ) -> dict[str, Any]:
        text = HeadlineService.clip_text(db, clip)
        provider_name = ""
        suggestions: list[dict[str, Any]] = []

        llm = ai_registry.language_model_provider(db, user.id)
        if llm is not None:
            try:
                context = {
                    "transcript": text[:1500],
                    "current_title": clip.title,
                    "tone": tone,
                    "platform": "vertical short-form video",
                    "max_characters": MAX_HEADLINE_CHARS,
                }
                variants = llm.headline_variants(context, count=count)
                provider_name = llm.name
                suggestions.extend(
                    {"text": HeadlineService._tidy(v), "source": "llm", "provider": llm.name}
                    for v in variants
                    if HeadlineService._tidy(v)
                )
                if refresh and clip.headline_variants:
                    clip.headline_variants = [s["text"] for s in suggestions][:8]
                    db.commit()
            except Exception as exc:
                log.warning("headline.llm_failed", clip_id=clip.id, error=str(exc)[:200])

        if len(suggestions) < count:
            existing = {s["text"].lower() for s in suggestions}
            for text_option in HeadlineService.extractive_suggestions(text, count=count, title=clip.title):
                if text_option.lower() in existing:
                    continue
                suggestions.append({"text": text_option, "source": "extractive", "provider": ""})
                if len(suggestions) >= count:
                    break

        stored = list(clip.headline_variants or [])
        for option in suggestions:
            if option["text"] not in stored:
                stored.append(option["text"])
        if suggestions:
            clip.headline_variants = stored[:10]
            db.commit()

        return {
            "clip_id": clip.id,
            "current": clip.headline,
            "suggestions": suggestions,
            "provider": provider_name,
            "message": "" if suggestions else "Not enough transcript text to suggest a headline yet.",
        }
