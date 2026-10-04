"""Anthropic Claude provider (messages API)."""
from __future__ import annotations

from app.core.config import settings
from app.services.ai.http import request_json
from app.services.ai.llm.base import BaseLLMProvider
from app.services.ai.types import LLMResult, LLMUsage, ProviderInfo


class AnthropicProvider(BaseLLMProvider):
    name = "anthropic"
    api_version = "2023-06-01"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ) -> None:
        self.api_key = (api_key if api_key is not None else settings.anthropic_api_key).strip()
        self.base_url = (base_url or settings.anthropic_base_url).rstrip("/")
        self.model = model or settings.anthropic_model
        self.is_local = False

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            name=self.name,
            kind="llm",
            available=bool(self.api_key),
            model=self.model,
            detail="" if self.api_key else "no API key configured",
            requires_key=True,
        )

    def _chat(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        temperature: float,
        json_mode: bool,
    ) -> LLMResult:
        system = "\n".join(m["content"] for m in messages if m["role"] == "system")
        conversation = [m for m in messages if m["role"] != "system"]
        body: dict = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [
                {"role": "assistant" if m["role"] == "assistant" else "user", "content": m["content"]}
                for m in conversation
            ],
        }
        if system:
            body["system"] = system
        if json_mode:
            # Nudge rather than a hard JSON mode: keeps compatibility with older models.
            body["system"] = (body.get("system", "") + "\nRespond with valid JSON only.").strip()
        payload = request_json(
            self.name,
            "POST",
            f"{self.base_url}/messages",
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": self.api_version,
                "Content-Type": "application/json",
            },
            json_body=body,
        )
        blocks = payload.get("content") or []
        text = "".join(str(block.get("text", "")) for block in blocks if block.get("type") == "text")
        usage = payload.get("usage") or {}
        return LLMResult(
            text=text,
            model=str(payload.get("model") or self.model),
            provider=self.name,
            usage=LLMUsage(
                prompt_tokens=int(usage.get("input_tokens") or 0),
                completion_tokens=int(usage.get("output_tokens") or 0),
                total_tokens=int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0),
            ),
            raw={"stop_reason": payload.get("stop_reason")},
        )
