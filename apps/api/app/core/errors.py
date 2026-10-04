"""Error taxonomy.

Every failure the API can produce maps to a stable machine-readable code and a
human message that is safe to show a user. Internal detail (stack traces, ffmpeg
stderr, provider payloads) stays in the structured log.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AppError(Exception):
    """Base class for all application errors."""

    message: str
    code: str = "internal_error"
    status_code: int = 500
    details: dict[str, Any] = field(default_factory=dict)
    retryable: bool = False

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.code}: {self.message}"


class ValidationError(AppError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, code="validation_error", status_code=422, details=details)


class AuthError(AppError):
    def __init__(self, message: str = "Authentication required.", code: str = "not_authenticated") -> None:
        super().__init__(message, code=code, status_code=401)


class PermissionError_(AppError):
    def __init__(self, message: str = "You do not have access to this resource.") -> None:
        super().__init__(message, code="forbidden", status_code=403)


class NotFoundError(AppError):
    def __init__(self, message: str = "Resource not found.", code: str = "not_found") -> None:
        super().__init__(message, code=code, status_code=404)


class ConflictError(AppError):
    def __init__(self, message: str, code: str = "conflict") -> None:
        super().__init__(message, code=code, status_code=409)


class RateLimitedError(AppError):
    def __init__(self, retry_after: int = 60) -> None:
        super().__init__(
            "Too many requests. Please slow down.",
            code="rate_limited",
            status_code=429,
            details={"retry_after": retry_after},
        )


class QuotaExceededError(AppError):
    def __init__(self, metric: str, limit: float, used: float) -> None:
        super().__init__(
            f"You have reached your plan limit for {metric.replace('_', ' ')}.",
            code="quota_exceeded",
            status_code=402,
            details={"metric": metric, "limit": limit, "used": used},
        )


class StorageError(AppError):
    def __init__(self, message: str = "Object storage is unavailable.", **details: Any) -> None:
        super().__init__(message, code="storage_error", status_code=503, details=details, retryable=True)


class MediaError(AppError):
    def __init__(self, message: str, code: str = "media_error", **details: Any) -> None:
        super().__init__(message, code=code, status_code=422, details=details, retryable=False)


class ProviderError(AppError):
    """Upstream AI provider failure — retryable, never surfaced raw."""

    def __init__(self, provider: str, message: str, *, retryable: bool = True) -> None:
        super().__init__(
            f"The {provider} provider could not complete the request.",
            code="provider_error",
            status_code=502,
            details={"provider": provider, "reason": message},
            retryable=retryable,
        )


class ProviderUnavailableError(AppError):
    """No provider is configured that can perform the requested task."""

    def __init__(self, task: str, guidance: str) -> None:
        super().__init__(
            f"No AI provider is configured for {task}.",
            code="provider_unavailable",
            status_code=503,
            details={"task": task, "guidance": guidance},
            retryable=False,
        )


class JobError(AppError):
    def __init__(self, message: str, code: str = "job_error", retryable: bool = True, **details: Any) -> None:
        super().__init__(message, code=code, status_code=500, details=details, retryable=retryable)


class CancelledError_(AppError):
    def __init__(self, message: str = "Job cancelled.") -> None:
        super().__init__(message, code="cancelled", status_code=409)

    def __str__(self) -> str:
        return self.message


def user_message(exc: BaseException) -> str:
    """Turn any exception into something safe to display."""
    if isinstance(exc, AppError):
        return exc.message
    return "Something went wrong while processing your request."
