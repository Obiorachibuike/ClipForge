"""The job system's durable record.

Jobs are the only way long work happens: HTTP requests enqueue a job and return
immediately, workers execute it and stream progress over WebSockets.
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, JSONColumn, TimestampMixin, UUIDMixin
from app.models.enums import JobStatus

if TYPE_CHECKING:
    from app.models.project import Project


class Job(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_status_priority", "status", "priority"),
        Index("ix_jobs_user_created", "user_id", "created_at"),
        Index("ix_jobs_project_type", "project_id", "type"),
    )

    type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), default=JobStatus.QUEUED.value, nullable=False, index=True)
    stage: Mapped[str] = mapped_column(String(64), default="queued", nullable=False)
    progress: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    message: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)

    user_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    project_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True
    )
    video_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("videos.id", ondelete="SET NULL"), nullable=True)
    clip_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("clips.id", ondelete="SET NULL"), nullable=True)

    payload: Mapped[dict] = mapped_column(JSONColumn, default=dict, nullable=False)
    result: Mapped[dict] = mapped_column(JSONColumn, default=dict, nullable=False)
    steps: Mapped[list] = mapped_column(JSONColumn, default=list, nullable=False)

    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    worker_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    error_code: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    error_message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    error_detail: Mapped[str] = mapped_column(Text, default="", nullable=False)
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    run_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    project: Mapped[Project | None] = relationship(back_populates="jobs")
