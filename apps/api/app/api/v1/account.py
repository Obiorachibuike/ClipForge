"""Account, settings, AI providers, usage and billing endpoints."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession, OptionalUser, require_admin
from app.core.config import settings
from app.core.errors import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.core.security import encrypt_secret, mask_secret
from app.models import AIProviderConfig, Plan, User
from app.schemas.auth import (
    AIProviderCreate,
    AIProviderOut,
    CapabilityOut,
    UserOut,
    UserUpdate,
)
from app.schemas.common import OkResponse
from app.services.ai import registry as ai_registry
from app.services.billing.plans import PLANS
from app.services.billing.provider import available_providers
from app.services.billing.subscription_service import SubscriptionService
from app.services.queue import get_queue
from app.services.storage import get_storage
from app.services.usage_service import UsageService

log = get_logger(__name__)
router = APIRouter(tags=["Account"])


# ----------------------------------------------------------------- settings ---
@router.get("/settings", response_model=dict)
def read_settings(db: DbSession, user: CurrentUser) -> dict:
    return {
        "user": UserOut.model_validate(user).model_dump(),
        "preferences": user.preferences or {},
        "privacy": {
            "mode": user.privacy_mode,
            "modes": [
                {
                    "key": "cloud",
                    "label": "ClipForge Cloud",
                    "detail": "Your video is uploaded to this ClipForge deployment and processed by its workers.",
                },
                {
                    "key": "private_ai",
                    "label": "Private AI providers",
                    "detail": (
                        "Transcription and language tasks run against AI providers you configure. The video file "
                        "still reaches this ClipForge server, and audio is sent to your provider."
                    ),
                },
            ],
        },
        "capabilities": _capabilities(),
    }


@router.patch("/settings", response_model=UserOut)
def update_settings(payload: UserUpdate, db: DbSession, user: CurrentUser) -> UserOut:
    if payload.name is not None:
        user.name = payload.name.strip()[:120]
    if payload.privacy_mode is not None:
        if payload.privacy_mode not in ("cloud", "private_ai"):
            raise ValidationError("Unknown privacy mode.", field="privacy_mode")
        user.privacy_mode = payload.privacy_mode
    if payload.default_aspect_ratio is not None:
        if payload.default_aspect_ratio not in ("9:16", "1:1", "16:9", "4:5", "3:4"):
            raise ValidationError("Unsupported aspect ratio.", field="default_aspect_ratio")
        user.default_aspect_ratio = payload.default_aspect_ratio
    if payload.default_caption_preset is not None:
        user.default_caption_preset = payload.default_caption_preset[:40]
    if payload.preferences is not None:
        user.preferences = {**(user.preferences or {}), **payload.preferences}
    db.commit()
    db.refresh(user)
    return UserOut.model_validate(user)


@router.get("/capabilities", response_model=CapabilityOut)
def capabilities(user: CurrentUser) -> CapabilityOut:
    return CapabilityOut(**_capabilities())


def _capabilities() -> dict:
    caps = settings.capabilities()
    queue = get_queue()
    return {
        "ffmpeg": caps["ffmpeg"],
        "ffprobe": caps["ffprobe"],
        "media_pipeline": caps["media_pipeline"],
        "vision": caps["vision"],
        "transcription": caps["transcription"],
        "llm": caps["llm"],
        "embeddings": caps["embeddings"],
        "storage_backend": get_storage().name,
        "queue_backend": queue.backend_name(),
        "billing": available_providers(),
        "processor": {
            "enabled": bool(settings.processor_enabled and settings.processor_url),
            "url": settings.processor_url,
        },
        "environment": settings.environment,
    }


# ------------------------------------------------------------ AI providers ---
@router.get("/settings/ai-providers", response_model=list[AIProviderOut])
def list_ai_providers(db: DbSession, user: CurrentUser) -> list[AIProviderOut]:
    rows = db.execute(
        select(AIProviderConfig).where(AIProviderConfig.user_id == user.id).order_by(AIProviderConfig.created_at)
    ).scalars()
    return [_provider_out(row) for row in rows]


@router.post("/settings/ai-providers", response_model=AIProviderOut, status_code=status.HTTP_201_CREATED)
def create_ai_provider(payload: AIProviderCreate, db: DbSession, user: CurrentUser) -> AIProviderOut:
    supported = {
        "llm": {"openai", "openai-compatible", "gemini", "anthropic", "custom", "ollama", "local"},
        "transcription": {"openai", "openai-compatible", "custom", "faster-whisper", "local"},
        "embedding": {"sentence-transformers", "tfidf", "custom"},
    }
    if payload.provider not in supported.get(payload.kind, set()):
        raise ValidationError(
            f"Provider '{payload.provider}' cannot be used for {payload.kind} tasks.", field="provider"
        )
    if payload.provider in {"openai", "openai-compatible", "gemini", "anthropic", "custom"} and not payload.api_key:
        raise ValidationError("An API key is required for this provider.", field="api_key")
    if payload.provider == "custom" and not payload.base_url:
        raise ValidationError("A base URL is required for a custom provider.", field="base_url")

    if payload.is_default:
        for existing in db.execute(
            select(AIProviderConfig).where(
                AIProviderConfig.user_id == user.id, AIProviderConfig.kind == payload.kind
            )
        ).scalars():
            existing.is_default = False

    row = AIProviderConfig(
        user_id=user.id,
        kind=payload.kind,
        provider=payload.provider,
        label=payload.label or payload.provider,
        model=payload.model,
        base_url=payload.base_url,
        api_key_encrypted=encrypt_secret(payload.api_key) if payload.api_key else "",
        is_default=payload.is_default,
        extra={},
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    ai_registry.clear_caches()
    log.info("ai_provider.created", user_id=user.id, kind=row.kind, provider=row.provider)
    return _provider_out(row)


@router.delete("/settings/ai-providers/{provider_id}", response_model=OkResponse)
def delete_ai_provider(provider_id: str, db: DbSession, user: CurrentUser) -> OkResponse:
    row = db.get(AIProviderConfig, provider_id)
    if row is None or row.user_id != user.id:
        raise NotFoundError("Provider not found.", code="provider_not_found")
    db.delete(row)
    db.commit()
    ai_registry.clear_caches()
    return OkResponse(message="Provider removed.")


@router.post("/settings/ai-providers/{provider_id}/test")
def test_ai_provider(provider_id: str, db: DbSession, user: CurrentUser) -> dict:
    """Perform a real call so the user knows whether their key works."""
    from app.services.ai.llm.base import BaseLLMProvider

    row = db.get(AIProviderConfig, provider_id)
    if row is None or row.user_id != user.id:
        raise NotFoundError("Provider not found.", code="provider_not_found")
    if row.kind != "llm":
        return {"ok": True, "detail": "This provider type is validated when it is used."}
    provider = ai_registry.language_model_provider(db, user.id)
    if provider is None:
        return {"ok": False, "detail": "No provider is active for this account."}
    try:
        result = provider.complete("Reply with the single word: ok", max_tokens=8, temperature=0.0)
        return {"ok": bool(result.text.strip()), "detail": result.text.strip()[:80], "provider": provider.name}
    except Exception as exc:  # surface a safe message, detail stays in logs
        log.warning("ai_provider.test_failed", provider_id=provider_id, error=str(exc)[:300])
        return {"ok": False, "detail": "The provider rejected the request. Check the key, model and base URL."}


def _provider_out(row: AIProviderConfig) -> AIProviderOut:
    from app.core.security import decrypt_secret

    plain = ""
    if row.api_key_encrypted:
        try:
            plain = decrypt_secret(row.api_key_encrypted)
        except Exception:  # pragma: no cover - corrupted key material
            plain = ""
    out = AIProviderOut.model_validate(row)
    out.has_api_key = bool(plain)
    out.masked_api_key = mask_secret(plain)
    return out


# -------------------------------------------------------------------- usage ---
@router.get("/usage", response_model=dict)
def read_usage(db: DbSession, user: CurrentUser) -> dict:
    return UsageService.summary(db, user)


@router.get("/usage/history")
def usage_history(db: DbSession, user: CurrentUser, months: int = 6, limit: int = 200) -> dict:
    from app.models import UsageRecord

    rows = db.execute(
        select(UsageRecord)
        .where(UsageRecord.user_id == user.id)
        .order_by(UsageRecord.created_at.desc())
        .limit(max(1, min(1000, limit)))
    ).scalars()
    return {
        "records": [
            {
                "metric": row.metric,
                "quantity": row.quantity,
                "unit": row.unit,
                "provider": row.provider,
                "project_id": row.project_id,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ]
    }


# ------------------------------------------------------------------ billing ---
@router.get("/billing/plans")
def billing_plans(user: OptionalUser) -> dict:
    """Public plan catalogue.

    Prices are marketing information and must render for signed-out visitors on
    the landing and pricing pages; only the `current` marker needs a session.
    """
    current_plan = user.plan if user is not None else ""
    return {
        "plans": [
            {
                "key": plan.key,
                "name": plan.name,
                "tagline": plan.tagline,
                "price_minor": plan.price_minor,
                "currency": plan.currency,
                "interval": plan.interval,
                "features": plan.features,
                "highlighted": plan.highlighted,
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
                "current": bool(current_plan) and plan.key == current_plan,
            }
            for plan in PLANS.values()
        ],
        "providers": available_providers(),
    }


@router.get("/billing/subscription")
def billing_subscription(db: DbSession, user: CurrentUser) -> dict:
    return SubscriptionService.current(db, user)


@router.post("/billing/checkout")
def billing_checkout(payload: dict, db: DbSession, user: CurrentUser) -> dict:
    plan = str(payload.get("plan") or "").lower()
    success_url = str(payload.get("success_url") or f"{settings.frontend_url}/settings?billing=success")
    cancel_url = str(payload.get("cancel_url") or f"{settings.frontend_url}/pricing?billing=cancelled")
    return SubscriptionService.start_checkout(db, user, plan, success_url=success_url, cancel_url=cancel_url)


@router.post("/billing/cancel")
def billing_cancel(db: DbSession, user: CurrentUser, at_period_end: bool = True) -> dict:
    subscription = SubscriptionService.cancel(db, user, at_period_end=at_period_end)
    return {"plan": subscription.plan, "status": subscription.status, "cancel_at_period_end": subscription.cancel_at_period_end}


@router.post("/billing/webhook/{provider_name}", include_in_schema=False)
async def billing_webhook(provider_name: str, request: Request, db: DbSession) -> dict:
    """Payment provider callback. Signature verified inside the provider."""
    payload = await request.body()
    headers = {key.lower(): value for key, value in request.headers.items()}
    result = SubscriptionService.handle_webhook(db, payload, headers)
    return result


@router.post("/billing/activate", include_in_schema=False)
def billing_activate(
    db: DbSession,
    admin: Annotated[User, Depends(require_admin)],
    payload: dict | None = None,
) -> dict:
    """Administrator-only manual activation (used when no gateway is configured)."""
    body = payload or {}
    user_id = str(body.get("user_id") or "")
    plan = str(body.get("plan") or Plan.PRO.value)
    target = db.get(User, user_id)
    if target is None:
        raise NotFoundError("User not found.", code="user_not_found")
    subscription = SubscriptionService.activate(db, target, plan, provider="manual")
    return {"user_id": target.id, "plan": subscription.plan, "status": subscription.status}
