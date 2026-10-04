"""Usage accounting: an append-only ledger plus derived aggregate helpers."""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Float, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, JSONColumn, TimestampMixin, UUIDMixin
from app.models.enums import UsageMetric

if TYPE_CHECKING:
    from app.models.account import User


class UsageRecord(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "usage_records"
    __table_args__ = (
        Index("ix_usage_user_metric_created", "user_id", "metric", "created_at"),
        Index("ix_usage_period", "user_id", "period_start"),
    )

    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    metric: Mapped[str] = mapped_column(String(40), default=UsageMetric.MINUTES_PROCESSED.value, nullable=False)
    quantity: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    unit: Mapped[str] = mapped_column(String(24), default="count", nullable=False)
    project_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    video_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    clip_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    job_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    provider: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    meta: Mapped[dict] = mapped_column(JSONColumn, default=dict, nullable=False)

    user: Mapped[User] = relationship()


class UsageCounter(Base, UUIDMixin, TimestampMixin):
    """Rolling counters per user/metric/billing-period for fast quota checks."""

    __tablename__ = "usage_counters"
    __table_args__ = (Index("ix_usage_counters_lookup", "user_id", "metric", "period_start"),)

    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    metric: Mapped[str] = mapped_column(String(40), nullable=False)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    total: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
