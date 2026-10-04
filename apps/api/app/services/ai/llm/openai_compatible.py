"""Chat completions over the OpenAI schema.

Covers OpenAI, Azure OpenAI gateways, Groq, Together, Ollama, vLLM, LM Studio
and any custom gateway that speaks /chat/completions.
"""
from __future__ import annotations

from app.core.config import settings
from app.services.ai.http import request_json
from app.services.ai.llm.base import BaseLLMProvider
from app.services.ai.types import LLMResult, LLMUsage, ProviderInfo


class OpenAICompatibleProvider(BaseLLMProvider):
    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        label: str = "openai",
        is_local: bool = False,
        requires_key: bool = True,
        extra_body: dict | None = None,
    ) -> None:
        self.api_key = (api_key if api_key is not None else settings.openai_api_key).strip()
        self.base_url = (base_url or settings.openai_base_url).rstrip("/")
        self.model = model or settings.openai_model
        self.name = label
        self.is_local = is_local
        self.requires_key = requires_key
        self.extra_body = extra_body or {}

    def info(self) -> ProviderInfo:
        available = bool(self.base_url and (self.api_key or not self.requires_key))
        return ProviderInfo(
            name=self.name,
            kind="llm",
            available=available,
            model=self.model,
            detail="" if available else "no API key configured",
            requires_key=self.requires_key,
            is_local=self.is_local,
        )

    def _chat(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        temperature: float,
        json_mode: bool,
    ) -> LLMResult:
        body: dict = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            **self.extra_body,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = request_json(
            self.name, "POST", f"{self.base_url}/chat/completions", headers=headers, json_body=body
        )
        choices = payload.get("choices") or []
        text = ""
        if choices:
            message = choices[0].get("message") or {}
            text = str(message.get("content") or "")
        usage = payload.get("usage") or {}
        return LLMResult(
            text=text,
            model=str(payload.get("model") or self.model),
            provider=self.name,
            usage=LLMUsage(
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                total_tokens=int(usage.get("total_tokens") or 0),
            ),
            raw={"finish_reason": (choices[0].get("finish_reason") if choices else None)},
        )


class OllamaProvider(OpenAICompatibleProvider):
    """Local model server. No API key, OpenAI-compatible endpoint."""

    def __init__(self, *, base_url: str = "http://localhost:11434/v1", model: str = "llama3.1") -> None:
        super().__init__(
            api_key="",
            base_url=base_url,
            model=model,
            label="ollama",
            is_local=True,
            requires_key=False,
            extra_body={},
        )


class CustomProvider(OpenAICompatibleProvider):
    """User-defined gateway (any base URL + key)."""

    def __init__(self, *, base_url: str, api_key: str, model: str, label: str = "custom") -> None:
        super().__init__(api_key=api_key, base_url=base_url, model=model, label=label, requires_key=True)
