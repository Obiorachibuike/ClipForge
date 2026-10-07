"""String enums used across models, schemas and services.

Stored as plain strings so the same schema works on SQLite and PostgreSQL and
so new values never require a migration.
"""
from __future__ import annotations

from enum import StrEnum


class Plan(StrEnum):
    FREE = "free"
    PRO = "pro"
    LIFETIME = "lifetime"
    ENTERPRISE = "enterprise"


class SubscriptionStatus(StrEnum):
    ACTIVE = "active"
    TRIALING = "trialing"
    PAST_DUE = "past_due"
    CANCELED = "canceled"
    EXPIRED = "expired"


class ProjectStatus(StrEnum):
    DRAFT = "draft"
    UPLOADING = "uploading"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"
    ARCHIVED = "archived"


class VideoStatus(StrEnum):
    PENDING = "pending"
    UPLOADING = "uploading"
    UPLOADED = "uploaded"
    PROBING = "probing"
    READY = "ready"
    FAILED = "failed"


class UploadStatus(StrEnum):
    INITIATED = "initiated"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    ABORTED = "aborted"
    FAILED = "failed"


class TranscriptStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    # Replaced by a newer run (e.g. the narration script changed). Kept for audit
    # and excluded from "current transcript" lookups.
    SUPERSEDED = "superseded"


class CandidateStatus(StrEnum):
    PENDING = "pending"
    KEPT = "kept"
    REJECTED = "rejected"
    CONVERTED = "converted"


class ClipStatus(StrEnum):
    DRAFT = "draft"
    READY = "ready"
    RENDERING = "rendering"
    RENDERED = "rendered"
    FAILED = "failed"


class RenderStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"
    RETRYING = "retrying"


class JobType(StrEnum):
    VIDEO_IMPORT_URL = "video.import_url"
    VIDEO_PROBE = "video.probe"
    VIDEO_TRANSCRIBE = "video.transcribe"
    VIDEO_ANALYZE_FRAMING = "video.analyze_framing"
    PROJECT_DISCOVER_CLIPS = "project.discover_clips"
    CLIP_RENDER = "clip.render"


class AnalysisKind(StrEnum):
    FRAMING = "framing"
    SCENES = "scenes"
    WAVEFORM = "waveform"
    ENERGY = "energy"


class AIProviderKind(StrEnum):
    TRANSCRIPTION = "transcription"
    LLM = "llm"
    EMBEDDING = "embedding"


class AIProviderName(StrEnum):
    LOCAL = "local"
    FASTER_WHISPER = "faster-whisper"
    OPENAI = "openai"
    OPENAI_COMPATIBLE = "openai-compatible"
    GEMINI = "gemini"
    ANTHROPIC = "anthropic"
    CUSTOM = "custom"
    OLLAMA = "ollama"
    NONE = "none"


class PrivacyMode(StrEnum):
    CLOUD = "cloud"
    PRIVATE_AI = "private_ai"


class UsageMetric(StrEnum):
    MINUTES_PROCESSED = "minutes_processed"
    VIDEOS_UPLOADED = "videos_uploaded"
    CLIPS_GENERATED = "clips_generated"
    RENDERS = "renders"
    STORAGE_BYTES = "storage_bytes"
    AI_REQUESTS = "ai_requests"


class RenderPreset(StrEnum):
    TIKTOK = "tiktok"
    YOUTUBE_SHORTS = "youtube_shorts"
    INSTAGRAM_REELS = "instagram_reels"
    FACEBOOK_REELS = "facebook_reels"
    GENERIC_VERTICAL = "generic_vertical"
    SQUARE = "square"
    LANDSCAPE = "landscape"
