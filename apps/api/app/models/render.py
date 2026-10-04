"""Render jobs and exports."""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, JSONColumn, TimestampMixin, UUIDMixin
from app.models.enums import RenderStatus

if TYPE_CHECKING:
    from app.models.clip import Clip


class RenderJob(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "render_jobs"
    __table_args__ = (
        Index("ix_render_jobs_clip_status", "clip_id", "status"),
        Index("ix_render_jobs_project_created", "project_id", "created_at"),
    )

    clip_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("clips.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    job_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True)

    preset: Mapped[str] = mapped_column(String(40), default="generic_vertical", nullable=False)
    aspect_ratio: Mapped[str] = mapped_column(String(12), default="9:16", nullable=False)
    width: Mapped[int] = mapped_column(Integer, default=1080, nullable=False)
    height: Mapped[int] = mapped_column(Integer, default=1920, nullable=False)
    fps: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    video_codec: Mapped[str] = mapped_column(String(32), default="h264", nullable=False)
    audio_codec: Mapped[str] = mapped_column(String(32), default="aac", nullable=False)
    bitrate: Mapped[str] = mapped_column(String(16), default="8M", nullable=False)
    container: Mapped[str] = mapped_column(String(16), default="mp4", nullable=False)

    status: Mapped[str] = mapped_column(String(32), default=RenderStatus.QUEUED.value, nullable=False, index=True)
    progress: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    stage: Mapped[str] = mapped_column(String(64), default="queued", nullable=False)
    message: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    storage_key: Mapped[str] = mapped_column(String(600), default="", nullable=False)
    thumbnail_key: Mapped[str] = mapped_column(String(600), default="", nullable=False)
    output_size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    output_duration: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    spec: Mapped[dict] = mapped_column(JSONColumn, default=dict, nullable=False)
    warnings: Mapped[list] = mapped_column(JSONColumn, default=list, nullable=False)
    error_code: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    error_message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    clip: Mapped[Clip] = relationship(back_populates="renders")
    exports: Mapped[list[Export]] = relationship(back_populates="render", cascade="all, delete-orphan")


class Export(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "exports"
    __table_args__ = (Index("ix_exports_project_created", "project_id", "created_at"),)

    render_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("render_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    clip_id: Mapped[str] = mapped_column(String(32), ForeignKey("clips.id", ondelete="CASCADE"), nullable=False)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    platform: Mapped[str] = mapped_column(String(40), default="generic_vertical", nullable=False)
    filename: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    storage_key: Mapped[str] = mapped_column(String(600), nullable=False)
    thumbnail_key: Mapped[str] = mapped_column(String(600), default="", nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    width: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    height: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    download_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_downloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    render: Mapped[RenderJob] = relationship(back_populates="exports")
    clip: Mapped[Clip] = relationship(back_populates="exports")
