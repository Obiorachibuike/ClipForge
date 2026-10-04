"""Caption engine services: cue building, style presets, safe text editing."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models import CaptionStyle, Clip, Transcript, TranscriptStatus, TranscriptWord, User
from app.services.media.ass import CAPTION_PRESETS, CaptionConfig, caption_cues

log = get_logger(__name__)

MAX_WORD_EDITS = 1000
MAX_WORD_LENGTH = 80


class CaptionService:
    # ------------------------------------------------------------- presets ---
    @staticmethod
    def presets() -> list[dict[str, Any]]:
        return [
            {
                "key": key,
                "name": preset["name"],
                "description": preset["description"],
                "config": CaptionConfig.from_dict({"preset": key}).to_dict(),
            }
            for key, preset in CAPTION_PRESETS.items()
        ]

    @staticmethod
    def ensure_system_styles(db: Session) -> list[CaptionStyle]:
        """Create the system preset rows once, then keep them in sync."""
        existing = {
            row.preset_key: row
            for row in db.execute(select(CaptionStyle).where(CaptionStyle.is_system.is_(True))).scalars()
        }
        created: list[CaptionStyle] = []
        for key, preset in CAPTION_PRESETS.items():
            config = CaptionConfig.from_dict({"preset": key}).to_dict()
            row = existing.get(key)
            if row is None:
                row = CaptionStyle(
                    user_id=None,
                    name=preset["name"],
                    preset_key=key,
                    description=preset["description"],
                    is_system=True,
                    config=config,
                )
                db.add(row)
                created.append(row)
            else:
                row.config = config
                row.description = preset["description"]
        if created:
            db.commit()
        return created

    @staticmethod
    def list_styles(db: Session, user: User) -> list[CaptionStyle]:
        CaptionService.ensure_system_styles(db)
        rows = db.execute(
            select(CaptionStyle)
            .where((CaptionStyle.user_id.is_(None)) | (CaptionStyle.user_id == user.id))
            .order_by(CaptionStyle.is_system.desc(), CaptionStyle.name)
        ).scalars()
        return list(rows)

    @staticmethod
    def create_style(db: Session, user: User, *, name: str, preset_key: str, description: str, config: dict) -> CaptionStyle:
        base = CaptionConfig.from_dict(config or {"preset": preset_key or "bold"})
        merged = {**base.to_dict(), **(config or {})}
        style = CaptionStyle(
            user_id=user.id,
            name=name.strip()[:80],
            preset_key=(preset_key or "")[:40],
            description=description[:255],
            is_system=False,
            config=CaptionConfig.from_dict(merged).to_dict(),
        )
        db.add(style)
        db.commit()
        db.refresh(style)
        return style

    @staticmethod
    def delete_style(db: Session, user: User, style_id: str) -> None:
        style = db.get(CaptionStyle, style_id)
        if style is None or style.user_id != user.id:
            raise NotFoundError("Caption style not found.", code="style_not_found")
        db.delete(style)
        db.commit()

    # ---------------------------------------------------------------- data ---
    @staticmethod
    def words_for_clip(db: Session, clip: Clip) -> tuple[list[dict[str, Any]], bool]:
        """Clip-relative words from the transcript, with user edits applied."""
        transcript = db.execute(
            select(Transcript)
            .where(Transcript.video_id == clip.video_id, Transcript.status == TranscriptStatus.COMPLETED.value)
            .order_by(Transcript.created_at.desc())
        ).scalars().first()
        if transcript is None:
            return [], False
        rows = list(
            db.execute(
                select(TranscriptWord)
                .where(
                    TranscriptWord.transcript_id == transcript.id,
                    TranscriptWord.end_time >= clip.start_time,
                    TranscriptWord.start_time <= clip.end_time,
                )
                .order_by(TranscriptWord.start_time)
            ).scalars()
        )
        edits = (clip.caption_overrides or {}).get("words") or {}
        words: list[dict[str, Any]] = []
        for index, row in enumerate(rows):
            start = max(0.0, row.start_time - clip.start_time)
            end = max(start + 0.05, min(row.end_time, clip.end_time) - clip.start_time)
            text = edits.get(str(index), row.word)
            words.append(
                {
                    "index": index,
                    "word_id": row.id,
                    "word": str(text),
                    "start": round(start, 3),
                    "end": round(end, 3),
                    "speaker": row.speaker or "",
                    "confidence": row.confidence,
                    "is_edited": str(index) in edits,
                }
            )
        for previous, current in zip(words, words[1:]):
            if current["start"] < previous["end"]:
                midpoint = round((previous["start"] + current["end"]) / 2.0, 3)
                previous["end"] = midpoint
                current["start"] = midpoint
        return words, True

    @staticmethod
    def apply_word_edits(clip: Clip, edits: dict[str, str] | None) -> dict[str, Any]:
        """Validate and store text edits. Timing is untouched by design."""
        if not edits:
            clip.caption_overrides = {**(clip.caption_overrides or {}), "words": {}}
            return clip.caption_overrides
        if len(edits) > MAX_WORD_EDITS:
            raise ValidationError(f"Too many edits in one request (max {MAX_WORD_EDITS}).", field="word_edits")
        cleaned: dict[str, str] = {}
        for key, value in edits.items():
            try:
                index = int(key)
            except (TypeError, ValueError):
                raise ValidationError("Word edits must be keyed by word index.", field="word_edits") from None
            if index < 0:
                raise ValidationError("Word index must be positive.", field="word_edits")
            text = str(value).strip()
            if not text:
                continue  # empty string removes the override, restoring the original
            cleaned[str(index)] = text[:MAX_WORD_LENGTH]
        clip.caption_overrides = {**(clip.caption_overrides or {}), "words": cleaned}
        return clip.caption_overrides

    @staticmethod
    def build_cues(words: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
        from app.services.media.ass import CaptionWord

        caption_words = [
            CaptionWord(
                text=word["word"],
                start=word["start"],
                end=word["end"],
                index=word["index"],
                speaker=word.get("speaker", ""),
                is_edited=bool(word.get("is_edited")),
            )
            for word in words
        ]
        cues = caption_cues(caption_words, config)
        by_index = {word["index"]: word for word in words}
        for cue in cues:
            for word in cue["words"]:
                source = by_index.get(word["index"])
                if source:
                    word["confidence"] = source.get("confidence", 0.0)
        return cues

    @staticmethod
    def resolve_config(clip: Clip, override: dict[str, Any] | None = None) -> dict[str, Any]:
        base = CaptionConfig.from_dict(clip.caption_config or {"preset": "bold"})
        merged = base.to_dict()
        if override:
            merged = {**merged, **{k: v for k, v in override.items() if v is not None}}
        return CaptionConfig.from_dict(merged).to_dict()
