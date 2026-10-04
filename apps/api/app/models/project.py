"""Projects, videos, chunked upload sessions and derived video analysis."""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, JSONColumn, TimestampMixin, UUIDMixin
from app.models.enums import AnalysisKind, ProjectStatus, UploadStatus, VideoStatus

if TYPE_CHECKING:
    from app.models.account import User
    from app.models.clip import Clip, ClipCandidate
    from app.models.job import Job
    from app.models.transcript import Transcript


class Project(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "projects"
    __table_args__ = (Index("ix_projects_user_updated", "user_id", "updated_at"),)

    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default=ProjectStatus.DRAFT.value, nullable=False, index=True)
    source_language: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    target_aspect_ratio: Mapped[str] = mapped_column(String(12), default="9:16", nullable=False)
    caption_style_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("caption_styles.id", ondelete="SET NULL"), nullable=True
    )
    privacy_mode: Mapped[str] = mapped_column(String(32), default="cloud", nullable=False)
    settings: Mapped[dict] = mapped_column(JSONColumn, default=dict, nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="projects")
    videos: Mapped[list[Video]] = relationship(
        back_populates="project", cascade="all, delete-orphan", order_by="Video.created_at"
    )
    candidates: Mapped[list[ClipCandidate]] = relationship(back_populates="project", cascade="all, delete-orphan")
    clips: Mapped[list[Clip]] = relationship(back_populates="project", cascade="all, delete-orphan")
    jobs: Mapped[list[Job]] = relationship(back_populates="project", cascade="all, delete-orphan")


class Video(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "videos"
    __table_args__ = (Index("ix_videos_project_status", "project_id", "status"),)

    project_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    original_filename: Mapped[str] = mapped_column(String(400), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(600), nullable=False)
    thumbnail_key: Mapped[str] = mapped_column(String(600), default="", nullable=False)
    content_type: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), default="", nullable=False)

    status: Mapped[str] = mapped_column(String(32), default=VideoStatus.PENDING.value, nullable=False, index=True)
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    width: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    height: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    fps: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    video_codec: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    audio_codec: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    audio_channels: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    audio_sample_rate: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    has_audio: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    rotation: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    probe: Mapped[dict] = mapped_column(JSONColumn, default=dict, nullable=False)
    error_message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # Optional narration script. When present, transcription aligns the real audio
    # to this text (exact word timings) instead of running automatic recognition.
    narration_script: Mapped[str] = mapped_column(Text, default="", nullable=False)

    project: Mapped[Project] = relationship(back_populates="videos")
    transcripts: Mapped[list[Transcript]] = relationship(
        back_populates="video", cascade="all, delete-orphan", order_by="Transcript.created_at"
    )
    analyses: Mapped[list[VideoAnalysis]] = relationship(back_populates="video", cascade="all, delete-orphan")

    @property
    def aspect_ratio(self) -> str:
        if not self.width or not self.height:
            return "16:9"
        if self.width == self.height:
            return "1:1"
        return "9:16" if self.height > self.width else "16:9"


class UploadSession(Base, UUIDMixin, TimestampMixin):
    """Chunked/resumable upload bookkeeping. Chunks are streamed straight to the
    storage backend; only bookkeeping lives in the database."""

    __tablename__ = "upload_sessions"
    __table_args__ = (UniqueConstraint("upload_id", name="uq_upload_sessions_upload_id"),)

    upload_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)

    filename: Mapped[str] = mapped_column(String(400), nullable=False)
    content_type: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    chunk_size: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_chunks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    received_chunks: Mapped[dict] = mapped_column(JSONColumn, default=dict, nullable=False)
    received_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default=UploadStatus.INITIATED.value, nullable=False, index=True)
    storage_key: Mapped[str] = mapped_column(String(600), nullable=False)
    parts_prefix: Mapped[str] = mapped_column(String(600), nullable=False)
    video_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("videos.id", ondelete="SET NULL"), nullable=True
    )
    error_message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class VideoAnalysis(Base, UUIDMixin, TimestampMixin):
    """Derived, recomputable analysis artifacts (framing keyframes, energy
    envelope, scene cuts). Stored as rows so they can be versioned and reused."""

    __tablename__ = "video_analysis"
    __table_args__ = (Index("ix_video_analysis_video_kind", "video_id", "kind"),)

    video_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("videos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(32), default=AnalysisKind.FRAMING.value, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="completed", nullable=False)
    detector: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    data: Mapped[dict] = mapped_column(JSONColumn, default=dict, nullable=False)
    error_message: Mapped[str] = mapped_column(Text, default="", nullable=False)

    video: Mapped[Video] = relationship(back_populates="analyses")
