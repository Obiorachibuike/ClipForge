"""Provider registry.

Resolution order for every task:
  1. a user-configured provider row (Private AI Mode), when the user chose one
  2. the deployment default from environment settings
  3. the next capable provider, or an explicit "unavailable" error

Providers are cached per configuration fingerprint.
"""
from __future__ import annotations

import threading
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import ProviderUnavailableError
from app.core.logging import get_logger
from app.core.security import decrypt_secret
from app.models import AIProviderConfig, AIProviderKind, AIProviderName
from app.services.ai.embeddings.tfidf import TfidfEmbeddings
from app.services.ai.types import ProviderInfo

log = get_logger(__name__)

_cache_lock = threading.Lock()
_llm_cache: dict[str, Any] = {}
_transcription_cache: dict[str, Any] = {}


# ------------------------------------------------------------- transcription ---
def script_alignment_provider(voice: str | None = None, rate: int | None = None):
    """Forced-alignment backend: word timings from a known script + real audio.

    This is not a recogniser. It measures where each script word actually lands in
    the audio and is therefore exact for narrations, voiceovers and read scripts.
    """
    from app.services.ai.transcription.alignment import ScriptAlignmentProvider

    kwargs: dict[str, Any] = {}
    if voice:
        kwargs["voice"] = voice
    if rate:
        kwargs["rate"] = rate
    return ScriptAlignmentProvider(**kwargs)


def transcription_provider(
    db: Session | None = None,
    user_id: str | None = None,
    *,
    script: str | None = None,
):
    """Pick the transcription backend (never fabricates a transcript).

    When the caller knows the spoken text (a pasted narration script), forced
    alignment is preferred because it yields exact word timings and avoids
    recognition errors entirely.
    """
    if script and script.strip() and settings.ai_script_alignment:
        if (settings.ai_transcription_provider or "auto").lower() != "none":
            alignment = script_alignment_provider()
            if alignment.info().available:
                log.info("ai.transcription.script_alignment", words=len(script.split()))
                return alignment
            log.info("ai.script_alignment_unavailable")
        else:
            log.info("ai.script_alignment_skipped", reason="transcription_disabled")

    config = _user_config(db, user_id, AIProviderKind.TRANSCRIPTION)
    if config is not None:
        return _build_transcription_from_config(config)

    preference = (settings.ai_transcription_provider or "auto").lower()
    if preference in ("faster-whisper", "local", "whisper", "auto"):
        from app.services.ai.transcription.faster_whisper import FasterWhisperProvider, availability

        ok, detail = availability()
        if ok or preference != "auto":
            return FasterWhisperProvider()
        log.info("ai.local_whisper_unavailable", detail=detail)

    if preference in ("openai", "custom", "openai-compatible", "auto"):
        from app.services.ai.transcription.openai_compatible import OpenAICompatibleTranscriptionProvider

        provider = OpenAICompatibleTranscriptionProvider(
            base_url=settings.custom_base_url if preference == "custom" else None,
            api_key=settings.custom_api_key if preference == "custom" else None,
            model=settings.custom_model or None if preference == "custom" else None,
            label="custom" if preference == "custom" else "openai",
        )
        if provider.info().available:
            return provider

    if preference == "none":
        raise ProviderUnavailableError(
            "transcription", "Transcription is disabled (AI_TRANSCRIPTION_PROVIDER=none)."
        )
    raise ProviderUnavailableError(
        "transcription",
        "Install faster-whisper with a locally cached model, or set OPENAI_API_KEY / "
        "configure a custom transcription provider in Settings → AI providers.",
    )


def _build_transcription_from_config(config: AIProviderConfig):
    provider = config.provider.lower()
    api_key = decrypt_secret(config.api_key_encrypted)
    if provider in (AIProviderName.OPENAI.value, AIProviderName.OPENAI_COMPATIBLE.value, AIProviderName.CUSTOM.value):
        from app.services.ai.transcription.openai_compatible import OpenAICompatibleTranscriptionProvider

        return OpenAICompatibleTranscriptionProvider(
            api_key=api_key,
            base_url=config.base_url or settings.openai_base_url,
            model=config.model or settings.openai_transcription_model,
            label=config.provider,
        )
    if provider in (AIProviderName.FASTER_WHISPER.value, AIProviderName.LOCAL.value):
        from app.services.ai.transcription.faster_whisper import FasterWhisperProvider

        return FasterWhisperProvider(model=config.model or None)
    raise ProviderUnavailableError("transcription", f"Provider '{config.provider}' cannot transcribe audio.")


# ------------------------------------------------------------------- llm ---
def language_model_provider(db: Session | None = None, user_id: str | None = None):
    """Return a language model provider, or None when none is configured.

    Returning None is meaningful: the discovery engine then runs in structural
    mode (measured signals only) and the UI says so. It never invents LLM copy.
    """
    config = _user_config(db, user_id, AIProviderKind.LLM)
    if config is not None:
        return _build_llm_from_config(config)

    preference = (settings.ai_llm_provider or "auto").lower()
    candidates: list[Any] = []
    if preference in ("openai", "openai-compatible", "auto") and settings.openai_api_key:
        from app.services.ai.llm.openai_compatible import OpenAICompatibleProvider

        candidates.append(OpenAICompatibleProvider(label="openai"))
    if preference in ("gemini", "auto") and settings.gemini_api_key:
        from app.services.ai.llm.gemini import GeminiProvider

        candidates.append(GeminiProvider())
    if preference in ("anthropic", "auto") and settings.anthropic_api_key:
        from app.services.ai.llm.anthropic import AnthropicProvider

        candidates.append(AnthropicProvider())
    if preference in ("custom", "auto") and settings.custom_base_url and settings.custom_api_key:
        from app.services.ai.llm.openai_compatible import CustomProvider

        candidates.append(
            CustomProvider(
                base_url=settings.custom_base_url,
                api_key=settings.custom_api_key,
                model=settings.custom_model or settings.openai_model,
            )
        )
    if preference == "ollama":
        from app.services.ai.llm.openai_compatible import OllamaProvider

        candidates.append(OllamaProvider())

    for candidate in candidates:
        if candidate.info().available:
            return candidate
    return None


def require_language_model(db: Session | None = None, user_id: str | None = None):
    provider = language_model_provider(db, user_id)
    if provider is None:
        raise ProviderUnavailableError(
            "language model tasks",
            "Add an API key (OPENAI_API_KEY / GEMINI_API_KEY / ANTHROPIC_API_KEY) or configure a "
            "custom/Ollama provider in Settings → AI providers.",
        )
    return provider


def _build_llm_from_config(config: AIProviderConfig):
    provider = config.provider.lower()
    api_key = decrypt_secret(config.api_key_encrypted)
    base_url = config.base_url or None
    model = config.model or None
    if provider in (AIProviderName.OPENAI.value, AIProviderName.OPENAI_COMPATIBLE.value):
        from app.services.ai.llm.openai_compatible import OpenAICompatibleProvider

        return OpenAICompatibleProvider(api_key=api_key, base_url=base_url, model=model, label=config.provider)
    if provider == AIProviderName.CUSTOM.value:
        from app.services.ai.llm.openai_compatible import CustomProvider

        return CustomProvider(base_url=base_url or "", api_key=api_key, model=model or settings.openai_model)
    if provider == AIProviderName.OLLAMA.value:
        from app.services.ai.llm.openai_compatible import OllamaProvider

        return OllamaProvider(base_url=base_url or "http://localhost:11434/v1", model=model or "llama3.1")
    if provider == AIProviderName.GEMINI.value:
        from app.services.ai.llm.gemini import GeminiProvider

        return GeminiProvider(api_key=api_key, base_url=base_url, model=model)
    if provider == AIProviderName.ANTHROPIC.value:
        from app.services.ai.llm.anthropic import AnthropicProvider

        return AnthropicProvider(api_key=api_key, base_url=base_url, model=model)
    raise ProviderUnavailableError("language model tasks", f"Provider '{config.provider}' is not supported.")


# -------------------------------------------------------------- embeddings ---
def embedding_provider(corpus: list[str] | None = None):
    """Neural embeddings when locally available, else real TF-IDF vectors."""
    preference = (settings.ai_embedding_provider or "auto").lower()
    if preference in ("sentence-transformers", "auto"):
        try:
            from app.services.ai.embeddings.sentence_transformers import (
                SentenceTransformerEmbeddings,
                model_cached,
                package_available,
            )

            if package_available() and (model_cached() or preference == "sentence-transformers"):
                provider = SentenceTransformerEmbeddings()
                if provider.info().available:
                    return provider
        except Exception as exc:  # pragma: no cover
            log.info("ai.embeddings.neural_unavailable", error=str(exc))
    provider = TfidfEmbeddings()
    if corpus:
        provider.fit(corpus)
    return provider


# ------------------------------------------------------------------ config ---
def _user_config(db: Session | None, user_id: str | None, kind: str) -> AIProviderConfig | None:
    if db is None or not user_id:
        return None
    stmt = (
        select(AIProviderConfig)
        .where(
            AIProviderConfig.user_id == user_id,
            AIProviderConfig.kind == kind,
            AIProviderConfig.is_enabled.is_(True),
        )
        .order_by(AIProviderConfig.is_default.desc(), AIProviderConfig.created_at.desc())
    )
    return db.execute(stmt).scalars().first()


# ------------------------------------------------------------- capability ---
def available_providers(settings_obj=settings) -> dict[str, Any]:
    """Everything the settings screen needs, with honest availability."""
    from app.services.ai.transcription.faster_whisper import availability as whisper_availability
    from app.services.ai.transcription.faster_whisper import package_available as whisper_installed

    whisper_ok, whisper_detail = whisper_availability()

    def llm_entry(provider: Any) -> dict[str, Any]:
        info: ProviderInfo = provider.info()
        return {
            "name": info.name,
            "available": info.available,
            "model": info.model,
            "detailed": info.detail,
            "requires_key": info.requires_key,
            "is_local": info.is_local,
        }

    llm_entries: list[dict[str, Any]] = []
    from app.services.ai.llm.anthropic import AnthropicProvider
    from app.services.ai.llm.gemini import GeminiProvider
    from app.services.ai.llm.openai_compatible import OllamaProvider, OpenAICompatibleProvider

    llm_entries.append(llm_entry(OpenAICompatibleProvider()))
    llm_entries.append(llm_entry(GeminiProvider()))
    llm_entries.append(llm_entry(AnthropicProvider()))
    llm_entries.append(
        llm_entry(
            OpenAICompatibleProvider(
                api_key=settings_obj.custom_api_key,
                base_url=settings_obj.custom_base_url or "http://localhost:8000/v1",
                model=settings_obj.custom_model or "custom-model",
                label="custom",
            )
        )
    )
    llm_entries.append(llm_entry(OllamaProvider()))

    from app.services.ai.embeddings.sentence_transformers import package_available as st_installed

    return {
        "transcription": {
            "local": {
                "name": "faster-whisper",
                "installed": whisper_installed(),
                "available": whisper_ok,
                "model": settings_obj.whisper_model,
                "detail": whisper_detail,
                "is_local": True,
            },
            "api": {
                "name": "openai-compatible",
                "available": bool(settings_obj.openai_api_key or settings_obj.custom_api_key),
                "model": settings_obj.openai_transcription_model,
                "is_local": False,
            },
            "active": (settings_obj.ai_transcription_provider or "auto"),
            "script_alignment": _alignment_capability(settings_obj),
        },
        "llm": {
            "providers": llm_entries,
            "configured": [entry["name"] for entry in llm_entries if entry["available"]],
            "active": settings_obj.ai_llm_provider,
        },
        "embeddings": {
            "neural_available": st_installed(),
            "active": "sentence-transformers" if st_installed() else "tfidf",
            "note": "TF-IDF lexical similarity is always available and needs no model download.",
        },
    }


def _alignment_capability(settings_obj=settings) -> dict[str, Any]:
    """Can this deployment align audio to a pasted script?"""
    try:
        from app.services.ai.transcription.alignment import espeak_available

        available = espeak_available()
    except Exception as exc:  # pragma: no cover - optional dependency
        log.info("ai.alignment_probe_failed", error=str(exc))
        available = False
    return {
        "available": bool(available and settings_obj.ai_script_alignment),
        "name": "script-alignment",
        "detail": (
            "Paste the spoken script and word timings come from aligning it to the real audio."
            if available
            else "Install espeakng-loader to enable script alignment."
        ),
        "is_local": True,
    }


def vision_capability(settings_obj=settings) -> dict[str, Any]:
    """Face detection / framing capabilities of this deployment."""
    import importlib.util

    from app.services.media.ffmpeg import have_ffmpeg

    detectors: list[dict[str, Any]] = []
    try:
        import cv2

        has_cascade = hasattr(cv2, "CascadeClassifier")
        detectors.append(
            {
                "name": "haar",
                "label": "OpenCV Haar cascade (bundled)",
                "available": has_cascade and have_ffmpeg(),
                "note": "frontal+profile face detection, no model download",
            }
        )
    except Exception:
        detectors.append({"name": "haar", "label": "OpenCV Haar cascade", "available": False, "note": "opencv not installed"})
    mediapipe_ok = importlib.util.find_spec("mediapipe") is not None
    detectors.append(
        {
            "name": "mediapipe",
            "label": "MediaPipe Face Detection",
            "available": mediapipe_ok,
            "note": "higher accuracy, requires the mediapipe package",
        }
    )
    detectors.append(
        {
            "name": "yunet",
            "label": "OpenCV YuNet (DNN)",
            "available": bool(settings_obj.vision_yunet_model),
            "note": "set VISION_YUNET_MODEL to enable",
        }
    )
    any_available = any(d["available"] for d in detectors)
    return {
        "enabled": bool(settings_obj.vision_enabled and any_available),
        "detectors": detectors,
        "active": settings_obj.vision_face_detector,
        "processor_sidecar": bool(settings_obj.processor_enabled and settings_obj.processor_url),
    }


def clear_caches() -> None:
    with _cache_lock:
        _llm_cache.clear()
        _transcription_cache.clear()
