"""Shared HTTP helper for AI providers: timeouts, bounded retries, safe errors."""
from __future__ import annotations

import json
import time
from typing import Any

import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.core.errors import ProviderError

log = get_logger(__name__)


def request_json(
    provider: str,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    files: dict[str, Any] | None = None,
    timeout: float | None = None,
    retries: int | None = None,
    expect_json: bool = True,
) -> Any:
    """Perform an HTTP request with retries; raise ProviderError on failure.

    The error message keeps upstream detail (logged server-side) out of the
    user-facing message handled by the API layer.
    """
    attempts = (retries if retries is not None else settings.ai_max_retries) + 1
    last_error: str = ""
    for attempt in range(attempts):
        try:
            with httpx.Client(timeout=timeout or settings.ai_request_timeout_seconds) as client:
                response = client.request(
                    method,
                    url,
                    headers=headers,
                    json=json_body,
                    data=data,
                    files=files,
                )
            if response.status_code >= 500 or response.status_code == 429:
                last_error = f"HTTP {response.status_code}: {response.text[:300]}"
                raise httpx.HTTPError(last_error)
            if response.status_code >= 400:
                detail = response.text[:300]
                # 4xx is a configuration problem: don't retry, report precisely.
                raise ProviderError(provider, f"HTTP {response.status_code}: {detail}", retryable=False)
            if not expect_json:
                return response.content
            try:
                return response.json()
            except json.JSONDecodeError as exc:
                raise ProviderError(provider, "Provider returned a non-JSON response.", retryable=False) from exc
        except ProviderError:
            raise
        except Exception as exc:
            last_error = str(exc)
            if attempt < attempts - 1:
                delay = 1.5 * (attempt + 1)
                log.warning("provider.retry", provider=provider, attempt=attempt + 1, error=last_error[:200])
                time.sleep(delay)
    raise ProviderError(provider, last_error or "unknown error")


def parse_json_loose(text: str) -> Any:
    """Parse JSON that may be wrapped in prose or code fences."""
    candidate = (text or "").strip()
    if candidate.startswith("```"):
        candidate = candidate.split("```")[1] if "```" in candidate[3:] else candidate.strip("`")
        candidate = candidate.removeprefix("json").strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("[", "]"), ("{", "}")):
        start = candidate.find(opener)
        end = candidate.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(candidate[start : end + 1])
            except json.JSONDecodeError:
                continue
    return None
