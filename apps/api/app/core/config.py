"""Application configuration.

Every tunable is an environment variable; nothing is hard-coded. The settings
object also owns *capability probing* so the UI can tell the truth about what
this deployment can actually do (which AI providers, whether local Whisper is
present, whether object storage is configured, ...).
"""
from __future__ import annotations

import os
import shutil
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[4]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------------------------------------------------------- core ---
    app_name: str = "ClipForge"
    environment: Literal["development", "test", "production"] = "development"
    debug: bool = False
    log_level: str = "INFO"
    log_json: bool = False
    api_prefix: str = "/api/v1"

    # ------------------------------------------------------------ security ---
    secret_key: str = Field(default="dev-only-insecure-secret-change-me")
    jwt_secret: str = Field(default="")
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 60 * 24 * 7  # session cookie lifetime (7d)
    session_cookie_name: str = "clipforge_session"
    csrf_cookie_name: str = "clipforge_csrf"
    csrf_header_name: str = "x-csrf-token"
    cookie_secure: bool = False
    cookie_domain: str | None = None
    cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    password_min_length: int = 8
    # Encryption key for provider API keys at rest (Fernet). Derived from
    # SECRET_KEY when not provided.
    encryption_key: str = ""

    # ---------------------------------------------------------------- data ---
    database_url: str = "sqlite:///./clipforge-dev.db"
    db_echo: bool = False
    db_pool_size: int = 10
    db_max_overflow: int = 20
    auto_create_tables: bool = True  # production should run Alembic instead

    redis_url: str = "redis://localhost:6379/0"
    # auto -> use redis when reachable, otherwise fall back to in-process
    queue_backend: Literal["auto", "redis", "memory"] = "auto"
    cache_backend: Literal["auto", "redis", "memory"] = "auto"

    # -------------------------------------------------------------- workers ---
    worker_inline: bool = True  # run a worker inside the API process (dev)
    worker_concurrency: int = 2
    worker_poll_interval: float = 0.5
    worker_max_attempts: int = 3
    worker_backoff_seconds: float = 5.0
    worker_job_timeout_minutes: int = 90
    render_concurrency: int = 1
    heartbeat_seconds: float = 1.5

    # ------------------------------------------------------------- storage ---
    storage_backend: Literal["auto", "local", "s3"] = "auto"
    storage_local_root: str = str(REPO_ROOT / ".data" / "storage")
    storage_max_bytes: int = 20 * 1024 * 1024 * 1024  # account quota (20 GB)
    s3_endpoint: str = ""
    s3_region: str = "us-east-1"
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_bucket: str = "clipforge"
    s3_prefix: str = ""
    s3_public_base_url: str = ""
    signed_url_ttl_seconds: int = 3600
    s3_path_style: bool = True

    # --------------------------------------------------------------- media ---
    media_ffmpeg_path: str = ""
    media_ffprobe_path: str = ""
    media_max_upload_bytes: int = 10 * 1024 * 1024 * 1024
    media_min_upload_bytes: int = 1024
    media_allowed_extensions: str = "mp4,mov,mkv,webm,m4v,avi,mp3,m4a,wav,flac,ogg,ts"
    media_allowed_mime_prefixes: str = "video/,audio/"
    media_probe_timeout_seconds: int = 120
    media_frame_sample_fps: float = 2.0
    media_max_analysis_frames: int = 900
    upload_chunk_size: int = 8 * 1024 * 1024
    upload_session_ttl_minutes: int = 60 * 24
    default_aspect_ratio: str = "9:16"

    # ------------------------------------------------------------ rendering ---
    render_video_codec: str = "libx264"
    render_audio_codec: str = "aac"
    render_preset: str = "medium"
    render_crf: int = 20
    render_default_bitrate: str = "8M"
    render_audio_bitrate: str = "192k"
    render_threads: int = 0  # 0 = ffmpeg auto
    font_directory: str = "/usr/share/fonts"

    # ------------------------------------------------------------------ ai ---
    ai_transcription_provider: str = "auto"  # auto|faster-whisper|openai|custom|none
    # Forced alignment against a supplied script: real word timings from the real
    # audio when the spoken text is known. Falls back to ASR when no script exists.
    ai_script_alignment: bool = True
    ai_llm_provider: str = "auto"  # auto|openai|gemini|anthropic|custom|ollama|none
    ai_embedding_provider: str = "auto"  # auto|sentence-transformers|tfidf
    whisper_model: str = "base"
    whisper_device: str = "auto"  # auto|cpu|cuda
    whisper_compute_type: str = "int8"
    whisper_beam_size: int = 5
    whisper_download_root: str = ""
    whisper_local_files_only: bool = False
    whisper_allow_fallback: bool = True  # allow openai provider fallback
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"
    openai_transcription_model: str = "whisper-1"
    gemini_api_key: str = ""
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    gemini_model: str = "gemini-1.5-flash"
    anthropic_api_key: str = ""
    anthropic_base_url: str = "https://api.anthropic.com/v1"
    anthropic_model: str = "claude-3-5-sonnet-latest"
    custom_api_key: str = ""
    custom_base_url: str = ""
    custom_model: str = ""
    ai_request_timeout_seconds: int = 120
    ai_max_retries: int = 2
    vision_enabled: bool = True
    vision_face_detector: str = "auto"  # auto|haar|mediapipe|yunet|none
    vision_yunet_model: str = ""
    processor_url: str = ""  # Rust processor sidecar (HTTP transport)
    processor_binary: str = ""  # path to the compiled sidecar (CLI transport)
    processor_enabled: bool = False
    processor_timeout_seconds: int = 300

    # ------------------------------------------------------------ frontends ---
    frontend_url: str = "http://localhost:5173"
    api_url: str = "http://localhost:8000"
    websocket_url: str = ""  # derived from api_url when empty
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # ---------------------------------------------------------- rate limits ---
    rate_limit_enabled: bool = True
    rate_limit_requests: int = 240  # per window per identity
    rate_limit_window_seconds: int = 60
    rate_limit_auth_requests: int = 20
    rate_limit_upload_requests: int = 120

    # -------------------------------------------------------------- billing ---
    billing_provider: str = "manual"  # manual|stripe|paystack|flutterwave
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    paystack_secret_key: str = ""
    flutterwave_secret_key: str = ""
    flutterwave_webhook_hash: str = ""
    billing_success_url: str = ""

    # ------------------------------------------------------------------ ops ---
    sentry_dsn: str = ""
    metrics_enabled: bool = True
    seed_demo_data: bool = False

    # ------------------------------------------------------------- verticals ---
    @field_validator("jwt_secret", mode="after")
    @classmethod
    def _fallback_jwt_secret(cls, v: str, info) -> str:  # noqa: ANN001
        if v:
            return v
        return info.data.get("secret_key", "dev-only-insecure-secret-change-me")

    # ------------------------------------------------------------- helpers ---
    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def allowed_extensions(self) -> set[str]:
        return {e.strip().lower().lstrip(".") for e in self.media_allowed_extensions.split(",") if e.strip()}

    @property
    def allowed_mime_prefixes(self) -> tuple[str, ...]:
        return tuple(p.strip() for p in self.media_allowed_mime_prefixes.split(",") if p.strip())

    @property
    def cors_origin_list(self) -> list[str]:
        origins = [o.strip() for o in self.cors_origins.split(",") if o.strip()]
        if self.frontend_url and self.frontend_url not in origins:
            origins.append(self.frontend_url)
        return origins

    @property
    def resolved_websocket_url(self) -> str:
        if self.websocket_url:
            return self.websocket_url
        return self.api_url.replace("https://", "wss://").replace("http://", "ws://")

    @property
    def local_storage_root(self) -> Path:
        return Path(self.storage_local_root).expanduser().resolve()

    @property
    def ffmpeg_path(self) -> str:
        return _resolve_binary(self.media_ffmpeg_path, "ffmpeg")

    @property
    def ffprobe_path(self) -> str:
        return _resolve_binary(self.media_ffprobe_path, "ffprobe")

    @property
    def has_ffprobe(self) -> bool:
        return bool(self.ffprobe_path)

    def capabilities(self) -> dict:
        """Truthful description of what this deployment can actually execute."""
        from app.services.ai import capabilities as ai_caps  # local import: heavy

        return {
            "ffmpeg": bool(self.ffmpeg_path),
            "ffprobe": self.has_ffprobe,
            "media_pipeline": bool(self.ffmpeg_path),
            "vision": ai_caps.vision_capability(self),
            **ai_caps.available_providers(self),
        }


def _resolve_binary(explicit: str, name: str) -> str:
    """Resolve a media binary: explicit env override -> imageio-ffmpeg -> PATH."""
    if explicit:
        return explicit if os.path.exists(explicit) else ""
    if name == "ffmpeg":
        try:  # PyPI-bundled static build (works in containers without apt access)
            import imageio_ffmpeg

            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:  # pragma: no cover - optional dependency
            pass
    found = shutil.which(name)
    return found or ""


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
