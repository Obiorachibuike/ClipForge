"""Plan catalogue.

Limits and prices are data, not code: the same definitions drive the pricing
page, quota enforcement and the subscription service.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.models import Plan


@dataclass(frozen=True)
class PlanLimits:
    minutes_processed: float
    videos_uploaded: int
    clips_generated: int
    renders: int
    storage_bytes: int
    ai_requests: int
    max_upload_bytes: int
    max_video_minutes: float
    watermark: bool = False
    priority_queue: bool = False


@dataclass(frozen=True)
class PlanDefinition:
    key: str
    name: str
    tagline: str
    price_minor: int  # in minor currency units (cents)
    currency: str
    interval: str  # month | once | forever
    limits: PlanLimits
    features: list[str] = field(default_factory=list)
    highlighted: bool = False


GB = 1024**3

PLANS: dict[str, PlanDefinition] = {
    Plan.FREE.value: PlanDefinition(
        key=Plan.FREE.value,
        name="Free",
        tagline="Try the full pipeline on a short video.",
        price_minor=0,
        currency="USD",
        interval="forever",
        limits=PlanLimits(
            minutes_processed=60,
            videos_uploaded=3,
            clips_generated=25,
            renders=15,
            storage_bytes=2 * GB,
            ai_requests=120,
            max_upload_bytes=1 * GB,
            max_video_minutes=20,
        ),
        features=[
            "60 processing minutes / month",
            "3 video uploads",
            "25 clip suggestions",
            "15 renders (720p–1080p)",
            "Word-level animated captions",
            "Structural clip discovery",
        ],
    ),
    Plan.PRO.value: PlanDefinition(
        key=Plan.PRO.value,
        name="Pro",
        tagline="For creators publishing every week.",
        price_minor=2900,
        currency="USD",
        interval="month",
        limits=PlanLimits(
            minutes_processed=1200,
            videos_uploaded=60,
            clips_generated=1000,
            renders=500,
            storage_bytes=100 * GB,
            ai_requests=5000,
            max_upload_bytes=10 * GB,
            max_video_minutes=240,
            priority_queue=True,
        ),
        features=[
            "1,200 processing minutes / month",
            "60 video uploads",
            "1,000 clip suggestions",
            "500 renders up to 1080p",
            "LLM-assisted titles + scoring",
            "Smart framing with speaker tracking",
            "Priority render queue",
            "Bring your own AI provider keys",
        ],
        highlighted=True,
    ),
    Plan.LIFETIME.value: PlanDefinition(
        key=Plan.LIFETIME.value,
        name="Lifetime",
        tagline="One payment, keep the tool.",
        price_minor=39900,
        currency="USD",
        interval="once",
        limits=PlanLimits(
            minutes_processed=20000,
            videos_uploaded=1000,
            clips_generated=20000,
            renders=10000,
            storage_bytes=500 * GB,
            ai_requests=50000,
            max_upload_bytes=20 * GB,
            max_video_minutes=300,
            priority_queue=True,
        ),
        features=[
            "20,000 processing minutes",
            "1,000 video uploads",
            "Unlimited-feeling clip suggestions",
            "10,000 renders",
            "All Pro features",
            "Lifetime updates",
        ],
    ),
    Plan.ENTERPRISE.value: PlanDefinition(
        key=Plan.ENTERPRISE.value,
        name="Enterprise",
        tagline="Self-hosted or volume deployments.",
        price_minor=0,
        currency="USD",
        interval="month",
        limits=PlanLimits(
            minutes_processed=100000,
            videos_uploaded=10000,
            clips_generated=100000,
            renders=100000,
            storage_bytes=2000 * GB,
            ai_requests=500000,
            max_upload_bytes=50 * GB,
            max_video_minutes=600,
            priority_queue=True,
        ),
        features=[
            "Negotiated volume limits",
            "Private AI Mode with your own endpoints",
            "Dedicated workers",
            "SSO / OAuth architecture",
        ],
    ),
}

METRIC_ALIASES = {
    "minutes_processed": "minutes_processed",
    "videos_uploaded": "videos_uploaded",
    "clips_generated": "clips_generated",
    "renders": "renders",
    "storage_bytes": "storage_bytes",
    "ai_requests": "ai_requests",
}


def plan_for(plan_key: str | None) -> PlanDefinition:
    return PLANS.get((plan_key or Plan.FREE.value).lower(), PLANS[Plan.FREE.value])


def limit_for(plan_key: str | None, metric: str) -> float:
    limits = plan_for(plan_key).limits
    attribute = METRIC_ALIASES.get(metric)
    if not attribute:
        return float("inf")
    return float(getattr(limits, attribute))
