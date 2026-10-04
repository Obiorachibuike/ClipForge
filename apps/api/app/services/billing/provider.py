"""Payment provider abstraction.

Business logic never imports a vendor SDK: it asks a `PaymentProvider` to create
a checkout session, verify a webhook or cancel a subscription. Adding a gateway
is a new class here plus a registry entry - no changes to subscription or usage
logic. No provider ever reports a fake success: an unconfigured gateway raises a
clear, actionable error.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.core.config import settings
from app.core.errors import AppError
from app.core.logging import get_logger

log = get_logger(__name__)


@dataclass
class CheckoutSession:
    provider: str
    reference: str
    checkout_url: str
    status: str = "pending"
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class WebhookEvent:
    kind: str  # subscription.activated | subscription.canceled | payment.succeeded | unknown
    plan: str | None = None
    user_reference: str | None = None
    subscription_reference: str | None = None
    amount_minor: int = 0
    currency: str = "USD"
    raw: dict[str, Any] = field(default_factory=dict)


class PaymentProvider(Protocol):
    name: str

    def available(self) -> bool: ...
    def create_checkout(self, *, user_id: str, email: str, plan: str, success_url: str, cancel_url: str) -> CheckoutSession: ...
    def verify_webhook(self, payload: bytes, headers: dict[str, str]) -> WebhookEvent: ...
    def cancel(self, reference: str) -> bool: ...


class BillingNotConfigured(AppError):
    def __init__(self, provider: str, hint: str) -> None:
        super().__init__(
            f"{provider} billing is not configured on this deployment.",
            code="billing_not_configured",
            status_code=503,
            details={"provider": provider, "hint": hint},
        )


class ManualProvider:
    """Default provider for self-hosted installs.

    Checkout is intentionally honest: it records the intent so an operator can
    activate the plan, and tells the user that no payment gateway is wired up.
    """

    name = "manual"

    def available(self) -> bool:
        return True

    def create_checkout(self, *, user_id: str, email: str, plan: str, success_url: str, cancel_url: str) -> CheckoutSession:
        raise BillingNotConfigured(
            "manual",
            "Set BILLING_PROVIDER=stripe|paystack|flutterwave and the matching secret key to enable online "
            "payments. Until then, plans are activated by an administrator.",
        )

    def verify_webhook(self, payload: bytes, headers: dict[str, str]) -> WebhookEvent:
        raise BillingNotConfigured("manual", "No webhook source is configured.")

    def cancel(self, reference: str) -> bool:
        return True


class StripeProvider:
    name = "stripe"

    def __init__(self, secret_key: str = "", webhook_secret: str = "") -> None:
        self.secret_key = secret_key or settings.stripe_secret_key
        self.webhook_secret = webhook_secret or settings.stripe_webhook_secret

    def available(self) -> bool:
        return bool(self.secret_key)

    def create_checkout(self, *, user_id: str, email: str, plan: str, success_url: str, cancel_url: str) -> CheckoutSession:
        if not self.available():
            raise BillingNotConfigured("stripe", "Set STRIPE_SECRET_KEY.")
        from app.services.billing.plans import plan_for

        definition = plan_for(plan)
        cycle = "payment" if definition.interval == "once" else "subscription"
        payload = {
            "mode": cycle,
            "success_url": success_url,
            "cancel_url": cancel_url,
            "client_reference_id": user_id,
            "customer_email": email,
        }
        if cycle == "payment":
            payload["line_items[0][price_data][currency]"] = definition.currency.lower()
            payload["line_items[0][price_data][product_data][name]"] = f"ClipForge {definition.name}"
            payload["line_items[0][price_data][unit_amount]"] = str(definition.price_minor)
            payload["line_items[0][quantity]"] = "1"
        else:
            payload["line_items[0][price_data][currency]"] = definition.currency.lower()
            payload["line_items[0][price_data][product_data][name]"] = f"ClipForge {definition.name}"
            payload["line_items[0][price_data][unit_amount]"] = str(definition.price_minor)
            payload["line_items[0][price_data][recurring][interval]"] = "month"
            payload["line_items[0][quantity]"] = "1"
        from app.services.ai.http import request_json

        response = request_json(
            "stripe",
            "POST",
            "https://api.stripe.com/v1/checkout/sessions",
            headers={"Authorization": f"Bearer {self.secret_key}"},
            data=payload,
        )
        return CheckoutSession(
            provider=self.name,
            reference=str(response.get("id", "")),
            checkout_url=str(response.get("url", "")),
            raw=response,
        )

    def verify_webhook(self, payload: bytes, headers: dict[str, str]) -> WebhookEvent:
        if self.webhook_secret:
            signature = headers.get("stripe-signature", "")
            if not _stripe_signature_valid(payload, signature, self.webhook_secret):
                raise AppError("Invalid webhook signature.", code="invalid_signature", status_code=400)
        data = json.loads(payload.decode("utf-8") or "{}")
        kind = str(data.get("type", ""))
        obj = (data.get("data") or {}).get("object") or {}
        mapping = {
            "checkout.session.completed": "subscription.activated",
            "customer.subscription.deleted": "subscription.canceled",
            "invoice.payment_succeeded": "payment.succeeded",
        }
        return WebhookEvent(
            kind=mapping.get(kind, "unknown"),
            plan=str((obj.get("metadata") or {}).get("plan", "") or ""),
            user_reference=str(obj.get("client_reference_id", "") or ""),
            subscription_reference=str(obj.get("subscription") or obj.get("id") or ""),
            amount_minor=int(obj.get("amount_total") or obj.get("amount_paid") or 0),
            currency=str(obj.get("currency", "usd")).upper(),
            raw=data,
        )

    def cancel(self, reference: str) -> bool:
        if not self.available() or not reference:
            return False
        from app.services.ai.http import request_json

        request_json(
            "stripe",
            "DELETE",
            f"https://api.stripe.com/v1/subscriptions/{reference}",
            headers={"Authorization": f"Bearer {self.secret_key}"},
        )
        return True


class PaystackProvider:
    name = "paystack"

    def __init__(self, secret_key: str = "") -> None:
        self.secret_key = secret_key or settings.paystack_secret_key

    def available(self) -> bool:
        return bool(self.secret_key)

    def create_checkout(self, *, user_id: str, email: str, plan: str, success_url: str, cancel_url: str) -> CheckoutSession:
        if not self.available():
            raise BillingNotConfigured("paystack", "Set PAYSTACK_SECRET_KEY.")
        from app.services.billing.plans import plan_for
        from app.services.ai.http import request_json

        definition = plan_for(plan)
        response = request_json(
            self.name,
            "POST",
            "https://api.paystack.co/transaction/initialize",
            headers={"Authorization": f"Bearer {self.secret_key}", "Content-Type": "application/json"},
            json_body={
                "email": email,
                "amount": definition.price_minor,
                "currency": definition.currency,
                "callback_url": success_url,
                "metadata": {"plan": plan, "user_id": user_id, "cancel_url": cancel_url},
            },
        )
        data = response.get("data") or {}
        return CheckoutSession(
            provider=self.name,
            reference=str(data.get("reference", "")),
            checkout_url=str(data.get("authorization_url", "")),
            raw=response,
        )

    def verify_webhook(self, payload: bytes, headers: dict[str, str]) -> WebhookEvent:
        signature = headers.get("x-paystack-signature", "")
        expected = hmac.new(self.secret_key.encode(), payload, hashlib.sha512).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise AppError("Invalid webhook signature.", code="invalid_signature", status_code=400)
        data = json.loads(payload.decode("utf-8") or "{}")
        event = str(data.get("event", ""))
        metadata = ((data.get("data") or {}).get("metadata") or {})
        return WebhookEvent(
            kind="payment.succeeded" if event == "charge.success" else "unknown",
            plan=str(metadata.get("plan", "")),
            user_reference=str(metadata.get("user_id", "")),
            subscription_reference=str((data.get("data") or {}).get("reference", "")),
            amount_minor=int((data.get("data") or {}).get("amount") or 0),
            currency=str((data.get("data") or {}).get("currency", "NGN")),
            raw=data,
        )

    def cancel(self, reference: str) -> bool:
        return self.available() and bool(reference)


class FlutterwaveProvider:
    name = "flutterwave"

    def __init__(self, secret_key: str = "") -> None:
        self.secret_key = secret_key or settings.flutterwave_secret_key

    def available(self) -> bool:
        return bool(self.secret_key)

    def create_checkout(self, *, user_id: str, email: str, plan: str, success_url: str, cancel_url: str) -> CheckoutSession:
        if not self.available():
            raise BillingNotConfigured("flutterwave", "Set FLUTTERWAVE_SECRET_KEY.")
        from app.services.billing.plans import plan_for
        from app.services.ai.http import request_json

        definition = plan_for(plan)
        response = request_json(
            self.name,
            "POST",
            "https://api.flutterwave.com/v3/payments",
            headers={"Authorization": f"Bearer {self.secret_key}", "Content-Type": "application/json"},
            json_body={
                "tx_ref": f"clipforge-{user_id}-{plan}",
                "amount": definition.price_minor / 100.0,
                "currency": definition.currency,
                "redirect_url": success_url,
                "customer": {"email": email},
                "meta": {"plan": plan, "user_id": user_id},
            },
        )
        data = response.get("data") or {}
        return CheckoutSession(
            provider=self.name,
            reference=str(data.get("tx_ref", "")),
            checkout_url=str(data.get("link", "")),
            raw=response,
        )

    def verify_webhook(self, payload: bytes, headers: dict[str, str]) -> WebhookEvent:
        signature = headers.get("verif-hash", "")
        if self.secret_key and not hmac.compare_digest(signature, settings.flutterwave_webhook_hash):
            raise AppError("Invalid webhook signature.", code="invalid_signature", status_code=400)
        data = json.loads(payload.decode("utf-8") or "{}")
        meta = (data.get("data") or {}).get("meta") or {}
        return WebhookEvent(
            kind="payment.succeeded" if data.get("status") == "successful" else "unknown",
            plan=str(meta.get("plan", "")),
            user_reference=str(meta.get("user_id", "")),
            amount_minor=int(float((data.get("data") or {}).get("amount") or 0) * 100),
            currency=str((data.get("data") or {}).get("currency", "USD")),
            raw=data,
        )

    def cancel(self, reference: str) -> bool:
        return self.available() and bool(reference)


def _stripe_signature_valid(payload: bytes, header: str, secret: str) -> bool:
    parts = dict(item.split("=", 1) for item in header.split(",") if "=" in item)
    timestamp, signature = parts.get("t", ""), parts.get("v1", "")
    if not timestamp or not signature:
        return False
    expected = hmac.new(secret.encode(), f"{timestamp}.{payload.decode('utf-8')}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


_REGISTRY: dict[str, type] = {
    "manual": ManualProvider,
    "stripe": StripeProvider,
    "paystack": PaystackProvider,
    "flutterwave": FlutterwaveProvider,
}


def get_payment_provider() -> PaymentProvider:
    key = (settings.billing_provider or "manual").lower()
    provider_class = _REGISTRY.get(key, ManualProvider)
    return provider_class()


def available_providers() -> list[dict[str, Any]]:
    result = []
    for key, provider_class in _REGISTRY.items():
        provider = provider_class()
        result.append({"name": key, "available": provider.available()})
    return result
