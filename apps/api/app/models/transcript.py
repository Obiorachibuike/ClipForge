"""Transcripts with segment and word level timing.

Word-level timestamps are the backbone of animated captions, so they are first
class rows rather than a blob.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, JSONColumn, TimestampMixin, UUIDMixin
from app.models.enums import TranscriptStatus

if TYPE_CHECKING:
    from app.models.project import Video


class Transcript(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "transcripts"
    __table_args__ = (Index("ix_transcripts_video_status", "video_id", "status"),)

    video_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("videos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)

    status: Mapped[str] = mapped_column(String(32), default=TranscriptStatus.PENDING.value, nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    model: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    language: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    language_probability: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    segment_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    word_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    speaker_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    full_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    avg_confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    is_primary: Mapped[bool] = mapped_column(default=True, nullable=False)
    error_message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    meta: Mapped[dict] = mapped_column(JSONColumn, default=dict, nullable=False)

    video: Mapped[Video] = relationship(back_populates="transcripts")
    segments: Mapped[list[TranscriptSegment]] = relationship(
        back_populates="transcript",
        cascade="all, delete-orphan",
        order_by="TranscriptSegment.index",
    )
    words: Mapped[list[TranscriptWord]] = relationship(
        back_populates="transcript",
        cascade="all, delete-orphan",
        order_by="TranscriptWord.index",
    )


class TranscriptSegment(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "transcript_segments"
    __table_args__ = (Index("ix_transcript_segments_transcript_start", "transcript_id", "start_time"),)

    transcript_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("transcripts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    start_time: Mapped[float] = mapped_column(Float, nullable=False)
    end_time: Mapped[float] = mapped_column(Float, nullable=False)
    text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    speaker: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    avg_confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    word_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # acoustics measured from the real audio signal (RMS energy, pitch estimate)
    energy: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    energy_variance: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    pause_before: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    speech_rate: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    transcript: Mapped[Transcript] = relationship(back_populates="segments")
    words: Mapped[list[TranscriptWord]] = relationship(
        back_populates="segment", cascade="all, delete-orphan", order_by="TranscriptWord.index"
    )


class TranscriptWord(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "transcript_words"
    __table_args__ = (
        Index("ix_transcript_words_transcript_start", "transcript_id", "start_time"),
        Index("ix_transcript_words_segment_index", "segment_id", "index"),
    )

    transcript_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("transcripts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    segment_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("transcript_segments.id", ondelete="CASCADE"), nullable=True, index=True
    )
    index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    word: Mapped[str] = mapped_column(String(191), nullable=False)
    start_time: Mapped[float] = mapped_column(Float, nullable=False)
    end_time: Mapped[float] = mapped_column(Float, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    speaker: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    is_edited: Mapped[bool] = mapped_column(default=False, nullable=False)

    transcript: Mapped[Transcript] = relationship(back_populates="words")
    segment: Mapped[TranscriptSegment | None] = relationship(back_populates="words")
