"""Usage accounting and quota enforcement."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import QuotaExceededError
from app.core.logging import get_logger
from app.models import Plan, UsageCounter, UsageRecord, User, utcnow
from app.services import events as ev
from app.services.billing.plans import limit_for, plan_for

log = get_logger(__name__)


class UsageService:
    @staticmethod
    def period_start(moment: datetime | None = None) -> datetime:
        moment = moment or utcnow()
        return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    # -------------------------------------------------------------- write ---
    @staticmethod
    def record(
        db: Session,
        *,
        user_id: str | None,
        metric: str,
        quantity: float,
        unit: str = "count",
        project_id: str | None = None,
        video_id: str | None = None,
        clip_id: str | None = None,
        job_id: str | None = None,
        provider: str = "",
        meta: dict[str, Any] | None = None,
        commit: bool = True,
    ) -> UsageRecord | None:
        if not user_id or quantity == 0:
            return None
        period = UsageService.period_start()
        record = UsageRecord(
            user_id=user_id,
            metric=metric,
            quantity=float(quantity),
            unit=unit,
            project_id=project_id,
            video_id=video_id,
            clip_id=clip_id,
            job_id=job_id,
            provider=provider,
            period_start=period,
            meta=meta or {},
        )
        db.add(record)

        counter = db.execute(
            select(UsageCounter).where(
                UsageCounter.user_id == user_id,
                UsageCounter.metric == metric,
                UsageCounter.period_start == period,
            )
        ).scalars().first()
        if counter is None:
            counter = UsageCounter(user_id=user_id, metric=metric, period_start=period, total=0.0)
            db.add(counter)
        counter.total = float(counter.total or 0.0) + float(quantity)
        if commit:
            db.commit()
            ev.get_event_bus().emit(
                ev.EVENT_USAGE_UPDATED,
                user_id=user_id,
                project_id=project_id,
                payload={"metric": metric, "total": counter.total, "unit": unit},
            )
        return record

    # -------------------------------------------------------------- read ---
    @staticmethod
    def totals(db: Session, user_id: str, *, period_start: datetime | None = None) -> dict[str, float]:
        period = period_start or UsageService.period_start()
        rows = db.execute(
            select(UsageCounter.metric, UsageCounter.total).where(
                UsageCounter.user_id == user_id, UsageCounter.period_start == period
            )
        ).all()
        totals = {metric: 0.0 for metric in ("minutes_processed", "videos_uploaded", "clips_generated", "renders", "storage_bytes", "ai_requests")}
        for metric, total in rows:
            totals[metric] = float(total or 0.0)
        return totals

    @staticmethod
    def storage_used_bytes(db: Session, user_id: str) -> int:
        """Current storage footprint (not a per-period counter)."""
        from app.models import Export, Video

        video_bytes = db.execute(
            select(func.coalesce(func.sum(Video.size_bytes), 0)).where(Video.user_id == user_id)
        ).scalar_one()
        export_bytes = db.execute(
            select(func.coalesce(func.sum(Export.size_bytes), 0)).where(
                Export.user_id == user_id, Export.deleted_at.is_(None)
            )
        ).scalar_one()
        return int(video_bytes or 0) + int(export_bytes or 0)

    @staticmethod
    def summary(db: Session, user: User) -> dict[str, Any]:
        plan = plan_for(user.plan)
        totals = UsageService.totals(db, user.id)
        storage = UsageService.storage_used_bytes(db, user.id)
        entries = []
        for metric, label, unit in (
            ("minutes_processed", "Minutes processed", "minutes"),
            ("videos_uploaded", "Videos uploaded", "videos"),
            ("clips_generated", "Clips generated", "clips"),
            ("renders", "Renders", "renders"),
            ("ai_requests", "AI requests", "requests"),
        ):
            limit = limit_for(user.plan, metric)
            used = totals.get(metric, 0.0)
            entries.append(
                {
                    "metric": metric,
                    "label": label,
                    "used": round(used, 2),
                    "limit": None if limit == float("inf") else limit,
                    "unit": unit,
                    "percent": round(min(100.0, (used / limit) * 100.0), 1) if limit else 0.0,
                    "remaining": None if limit == float("inf") else max(0.0, limit - used),
                }
            )
        storage_limit = limit_for(user.plan, "storage_bytes")
        entries.append(
            {
                "metric": "storage_bytes",
                "label": "Storage used",
                "used": storage,
                "limit": None if storage_limit == float("inf") else storage_limit,
                "unit": "bytes",
                "percent": round(min(100.0, (storage / storage_limit) * 100.0), 1) if storage_limit else 0.0,
                "remaining": None if storage_limit == float("inf") else max(0.0, storage_limit - storage),
            }
        )
        return {
            "plan": plan.key,
            "plan_name": plan.name,
            "period_start": UsageService.period_start().isoformat(),
            "metrics": entries,
        }

    # ------------------------------------------------------------ enforce ---
    @staticmethod
    def check(db: Session, user: User, metric: str, amount: float) -> None:
        """Raise QuotaExceededError when this operation would exceed the plan."""
        limit = limit_for(user.plan, metric)
        if limit == float("inf"):
            return
        if metric == "storage_bytes":
            used = UsageService.storage_used_bytes(db, user.id)
        else:
            used = UsageService.totals(db, user.id).get(metric, 0.0)
        if used + amount > limit:
            raise QuotaExceededError(metric, limit, used)

    @staticmethod
    def check_upload(db: Session, user: User, size_bytes: int, duration_seconds: float | None = None) -> None:
        plan = plan_for(user.plan)
        if size_bytes > plan.limits.max_upload_bytes:
            raise QuotaExceededError(
                "upload size", plan.limits.max_upload_bytes, float(size_bytes)
            )
        UsageService.check(db, user, "storage_bytes", float(size_bytes))
        UsageService.check(db, user, "videos_uploaded", 1.0)
        if duration_seconds:
            minutes = duration_seconds / 60.0
            if minutes > plan.limits.max_video_minutes:
                raise QuotaExceededError("video duration", plan.limits.max_video_minutes, minutes)
            UsageService.check(db, user, "minutes_processed", minutes)

    @staticmethod
    def plan_payload(user: User) -> dict[str, Any]:
        plan = plan_for(user.plan)
        return {
            "key": plan.key,
            "name": plan.name,
            "tagline": plan.tagline,
            "price_minor": plan.price_minor,
            "currency": plan.currency,
            "interval": plan.interval,
            "features": plan.features,
            "limits": {
                "minutes_processed": plan.limits.minutes_processed,
                "videos_uploaded": plan.limits.videos_uploaded,
                "clips_generated": plan.limits.clips_generated,
                "renders": plan.limits.renders,
                "storage_bytes": plan.limits.storage_bytes,
                "ai_requests": plan.limits.ai_requests,
                "max_upload_bytes": plan.limits.max_upload_bytes,
                "max_video_minutes": plan.limits.max_video_minutes,
                "priority_queue": plan.limits.priority_queue,
            },
        }
