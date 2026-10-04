"""Google Gemini provider (generateContent)."""
from __future__ import annotations

from app.core.config import settings
from app.services.ai.http import request_json
from app.services.ai.llm.base import BaseLLMProvider
from app.services.ai.types import LLMResult, LLMUsage, ProviderInfo


class GeminiProvider(BaseLLMProvider):
    name = "gemini"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ) -> None:
        self.api_key = (api_key if api_key is not None else settings.gemini_api_key).strip()
        self.base_url = (base_url or settings.gemini_base_url).rstrip("/")
        self.model = model or settings.gemini_model
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
        system_parts: list[str] = []
        contents: list[dict] = []
        for message in messages:
            if message["role"] == "system":
                system_parts.append(message["content"])
            else:
                contents.append(
                    {"role": "user" if message["role"] == "user" else "model", "parts": [{"text": message["content"]}]}
                )
        body: dict = {
            "contents": contents,
            "generationConfig": {"maxOutputTokens": max_tokens, "temperature": temperature},
        }
        if system_parts:
            body["systemInstruction"] = {"parts": [{"text": "\n".join(system_parts)}]}
        if json_mode:
            body["generationConfig"]["responseMimeType"] = "application/json"
        payload = request_json(
            self.name,
            "POST",
            f"{self.base_url}/models/{self.model}:generateContent?key={self.api_key}",
            headers={"Content-Type": "application/json"},
            json_body=body,
        )
        candidates = payload.get("candidates") or []
        text = ""
        if candidates:
            parts = (candidates[0].get("content") or {}).get("parts") or []
            text = "".join(str(part.get("text", "")) for part in parts)
        usage = payload.get("usageMetadata") or {}
        return LLMResult(
            text=text,
            model=self.model,
            provider=self.name,
            usage=LLMUsage(
                prompt_tokens=int(usage.get("promptTokenCount") or 0),
                completion_tokens=int(usage.get("candidatesTokenCount") or 0),
                total_tokens=int(usage.get("totalTokenCount") or 0),
            ),
            raw={"finish_reason": (candidates[0].get("finishReason") if candidates else None)},
        )
