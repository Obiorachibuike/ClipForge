"""Subscription lifecycle, independent of any payment vendor."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models import Payment, Plan, Subscription, SubscriptionStatus, User, utcnow
from app.services.billing.plans import PLANS, plan_for
from app.services.billing.provider import get_payment_provider

log = get_logger(__name__)


class SubscriptionService:
    @staticmethod
    def get_or_create(db: Session, user: User) -> Subscription:
        subscription = db.execute(
            select(Subscription).where(Subscription.user_id == user.id).order_by(Subscription.created_at.desc())
        ).scalars().first()
        if subscription is None:
            subscription = Subscription(
                user_id=user.id,
                plan=user.plan or Plan.FREE.value,
                status=SubscriptionStatus.ACTIVE.value,
                provider="manual",
                current_period_start=utcnow(),
                current_period_end=None,
            )
            db.add(subscription)
            db.commit()
            db.refresh(subscription)
        return subscription

    @staticmethod
    def current(db: Session, user: User) -> dict:
        subscription = SubscriptionService.get_or_create(db, user)
        definition = plan_for(subscription.plan)
        return {
            "plan": subscription.plan,
            "plan_name": definition.name,
            "status": subscription.status,
            "provider": subscription.provider,
            "provider_reference": subscription.provider_reference,
            "current_period_start": subscription.current_period_start.isoformat() if subscription.current_period_start else None,
            "current_period_end": subscription.current_period_end.isoformat() if subscription.current_period_end else None,
            "cancel_at_period_end": subscription.cancel_at_period_end,
            "price_minor": definition.price_minor,
            "currency": definition.currency,
            "interval": definition.interval,
        }

    @staticmethod
    def start_checkout(db: Session, user: User, plan_key: str, *, success_url: str, cancel_url: str) -> dict:
        plan_key = (plan_key or "").lower()
        if plan_key not in PLANS:
            raise ValidationError("Unknown plan.", plan=plan_key)
        if plan_key == Plan.FREE.value:
            SubscriptionService.activate(db, user, Plan.FREE.value, provider="manual")
            return {"activated": True, "plan": Plan.FREE.value}
        provider = get_payment_provider()
        session = provider.create_checkout(
            user_id=user.id, email=user.email, plan=plan_key, success_url=success_url, cancel_url=cancel_url
        )
        subscription = SubscriptionService.get_or_create(db, user)
        subscription.provider = provider.name
        subscription.provider_reference = session.reference
        subscription.status = SubscriptionStatus.TRIALING.value
        db.commit()
        return {
            "activated": False,
            "provider": provider.name,
            "reference": session.reference,
            "checkout_url": session.checkout_url,
            "plan": plan_key,
        }

    @staticmethod
    def activate(
        db: Session,
        user: User,
        plan_key: str,
        *,
        provider: str = "manual",
        provider_reference: str = "",
        period_days: int = 31,
    ) -> Subscription:
        subscription = SubscriptionService.get_or_create(db, user)
        subscription.plan = plan_key
        subscription.status = SubscriptionStatus.ACTIVE.value
        subscription.provider = provider
        if provider_reference:
            subscription.provider_reference = provider_reference
        subscription.current_period_start = utcnow()
        definition = plan_for(plan_key)
        subscription.current_period_end = (
            None if definition.interval in ("once", "forever") else utcnow() + timedelta(days=period_days)
        )
        subscription.cancel_at_period_end = False
        user.plan = plan_key
        db.commit()
        db.refresh(subscription)
        log.info("billing.activated", user_id=user.id, plan=plan_key, provider=provider)
        return subscription

    @staticmethod
    def cancel(db: Session, user: User, *, at_period_end: bool = True) -> Subscription:
        subscription = SubscriptionService.get_or_create(db, user)
        if at_period_end:
            subscription.cancel_at_period_end = True
            db.commit()
            return subscription
        provider = get_payment_provider()
        if subscription.provider_reference and subscription.provider != "manual":
            try:
                provider.cancel(subscription.provider_reference)
            except Exception as exc:
                log.warning("billing.cancel_remote_failed", error=str(exc)[:200])
        subscription.plan = Plan.FREE.value
        subscription.status = SubscriptionStatus.CANCELED.value
        subscription.cancel_at_period_end = False
        user.plan = Plan.FREE.value
        db.commit()
        return subscription

    @staticmethod
    def record_payment(
        db: Session,
        *,
        user: User,
        amount_minor: int,
        currency: str,
        provider: str,
        reference: str,
        description: str = "",
    ) -> Payment:
        subscription = SubscriptionService.get_or_create(db, user)
        payment = Payment(
            user_id=user.id,
            subscription_id=subscription.id,
            provider=provider,
            provider_reference=reference,
            amount_minor=amount_minor,
            currency=currency,
            status="succeeded",
            description=description[:255],
        )
        db.add(payment)
        db.commit()
        return payment

    @staticmethod
    def handle_webhook(db: Session, payload: bytes, headers: dict[str, str]) -> dict:
        provider = get_payment_provider()
        event = provider.verify_webhook(payload, headers)
        if event.kind == "unknown":
            return {"handled": False, "reason": "unrecognised event"}
        user = None
        if event.user_reference:
            user = db.get(User, event.user_reference)
        if user is None:
            return {"handled": False, "reason": "unknown user reference"}
        if event.kind == "subscription.activated" and event.plan:
            SubscriptionService.activate(
                db, user, event.plan, provider=provider.name, provider_reference=event.subscription_reference or ""
            )
        elif event.kind == "subscription.canceled":
            SubscriptionService.cancel(db, user, at_period_end=False)
        elif event.kind == "payment.succeeded":
            if event.plan:
                SubscriptionService.activate(
                    db, user, event.plan, provider=provider.name, provider_reference=event.subscription_reference or ""
                )
            SubscriptionService.record_payment(
                db,
                user=user,
                amount_minor=event.amount_minor,
                currency=event.currency,
                provider=provider.name,
                reference=event.subscription_reference or "",
                description=f"{provider.name} payment",
            )
        db.commit()
        return {"handled": True, "kind": event.kind, "plan": event.plan}

    @staticmethod
    def expire_periods(db: Session) -> int:
        """Downgrade subscriptions whose period ended (run from the worker)."""
        now = datetime.now(UTC)
        expired = 0
        rows = db.execute(
            select(Subscription).where(
                Subscription.status == SubscriptionStatus.ACTIVE.value,
                Subscription.current_period_end.isnot(None),
                Subscription.current_period_end < now,
            )
        ).scalars()
        for subscription in rows:
            user = db.get(User, subscription.user_id)
            subscription.status = SubscriptionStatus.EXPIRED.value
            subscription.plan = Plan.FREE.value
            if user:
                user.plan = Plan.FREE.value
            expired += 1
        if expired:
            db.commit()
            log.info("billing.subscriptions_expired", count=expired)
        return expired
