"""Clip candidates (AI discoveries) and clips (user-approved, editable)."""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, JSONColumn, TimestampMixin, UUIDMixin
from app.models.enums import CandidateStatus, ClipStatus

if TYPE_CHECKING:
    from app.models.project import Project, Video
    from app.models.render import Export, RenderJob


class ClipCandidate(Base, UUIDMixin, TimestampMixin):
    """A moment the discovery engine actually measured. `scores` holds the
    per-signal breakdown so a score can always be explained to the user."""

    __tablename__ = "clip_candidates"
    __table_args__ = (
        Index("ix_clip_candidates_project_score", "project_id", "score"),
        Index("ix_clip_candidates_project_status", "project_id", "status"),
    )

    project_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    video_id: Mapped[str] = mapped_column(String(32), ForeignKey("videos.id", ondelete="CASCADE"), nullable=False)
    transcript_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("transcripts.id", ondelete="SET NULL"), nullable=True
    )

    start_time: Mapped[float] = mapped_column(Float, nullable=False)
    end_time: Mapped[float] = mapped_column(Float, nullable=False)
    duration: Mapped[float] = mapped_column(Float, nullable=False)
    start_word_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    end_word_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    title: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    hook: Mapped[str] = mapped_column(Text, default="", nullable=False)
    reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    score: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    scores: Mapped[dict] = mapped_column(JSONColumn, default=dict, nullable=False)
    keywords: Mapped[list] = mapped_column(JSONColumn, default=list, nullable=False)
    category: Mapped[str] = mapped_column(String(40), default="insight", nullable=False)
    transcript_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    speaker: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default=CandidateStatus.PENDING.value, nullable=False, index=True)
    analyzer: Mapped[str] = mapped_column(String(64), default="structural-v1", nullable=False)
    rank: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    meta: Mapped[dict] = mapped_column(JSONColumn, default=dict, nullable=False)

    project: Mapped[Project] = relationship(back_populates="candidates")
    video: Mapped[Video] = relationship()
    clips: Mapped[list[Clip]] = relationship(back_populates="candidate")


class Clip(Base, UUIDMixin, TimestampMixin):
    """The working unit a user edits and renders. All edit state is stored in
    JSON columns describing the render graph, not in the browser."""

    __tablename__ = "clips"
    __table_args__ = (
        Index("ix_clips_project_status", "project_id", "status"),
        Index("ix_clips_user_updated", "user_id", "updated_at"),
    )

    project_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    video_id: Mapped[str] = mapped_column(String(32), ForeignKey("videos.id", ondelete="CASCADE"), nullable=False)
    candidate_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("clip_candidates.id", ondelete="SET NULL"), nullable=True
    )
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    title: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    headline: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    headline_position: Mapped[str] = mapped_column(String(24), default="top", nullable=False)
    headline_style: Mapped[dict] = mapped_column(JSONColumn, default=dict, nullable=False)
    headline_variants: Mapped[list] = mapped_column(JSONColumn, default=list, nullable=False)

    start_time: Mapped[float] = mapped_column(Float, nullable=False)
    end_time: Mapped[float] = mapped_column(Float, nullable=False)
    duration: Mapped[float] = mapped_column(Float, nullable=False)
    aspect_ratio: Mapped[str] = mapped_column(String(12), default="9:16", nullable=False)

    # render-graph state (see app.services.media.render_spec)
    crop: Mapped[dict] = mapped_column(JSONColumn, default=dict, nullable=False)
    background: Mapped[dict] = mapped_column(JSONColumn, default=dict, nullable=False)
    caption_config: Mapped[dict] = mapped_column(JSONColumn, default=dict, nullable=False)
    audio_config: Mapped[dict] = mapped_column(JSONColumn, default=dict, nullable=False)
    caption_style_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("caption_styles.id", ondelete="SET NULL"), nullable=True
    )
    caption_overrides: Mapped[dict] = mapped_column(JSONColumn, default=dict, nullable=False)

    status: Mapped[str] = mapped_column(String(32), default=ClipStatus.DRAFT.value, nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(32), default="candidate", nullable=False)
    is_favorite: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_rendered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    render_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)

    project: Mapped[Project] = relationship(back_populates="clips")
    video: Mapped[Video] = relationship()
    candidate: Mapped[ClipCandidate | None] = relationship(back_populates="clips")
    caption_style: Mapped[CaptionStyle | None] = relationship()
    renders: Mapped[list[RenderJob]] = relationship(
        back_populates="clip", cascade="all, delete-orphan", order_by="desc(RenderJob.created_at)"
    )
    exports: Mapped[list[Export]] = relationship(back_populates="clip", cascade="all, delete-orphan")


class CaptionStyle(Base, UUIDMixin, TimestampMixin):
    """Caption presets. `user_id IS NULL` marks a system preset."""

    __tablename__ = "caption_styles"
    __table_args__ = (Index("ix_caption_styles_user", "user_id", "name"),)

    user_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    preset_key: Mapped[str] = mapped_column(String(40), default="", nullable=False, index=True)
    description: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_favorite: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    config: Mapped[dict] = mapped_column(JSONColumn, default=dict, nullable=False)
