"""Clip, caption, headline, render and export schemas."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.schemas.common import ORMModel


# ---------------------------------------------------------------- candidates ---
class CandidateOut(ORMModel):
    id: str
    project_id: str
    video_id: str
    start_time: float
    end_time: float
    duration: float
    title: str
    hook: str
    reason: str
    score: int
    scores: dict[str, float] = Field(default_factory=dict)
    keywords: list[str] = Field(default_factory=list)
    category: str
    transcript_text: str
    speaker: str = ""
    status: str
    analyzer: str
    rank: int
    meta: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class CandidateUpdate(BaseModel):
    status: str | None = Field(default=None, pattern="^(pending|kept|rejected|converted)$")
    title: str | None = Field(default=None, max_length=300)
    hook: str | None = Field(default=None, max_length=500)


class CandidateBulkUpdate(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=200)
    status: str = Field(pattern="^(pending|kept|rejected)$")


class CandidateScoreDetail(BaseModel):
    candidate_id: str
    score: int
    components: dict[str, float]
    weights: dict[str, float]
    analyzer: str
    explanation: str


# -------------------------------------------------------------------- clips ---
class ClipCreate(BaseModel):
    candidate_id: str | None = None
    start_time: float | None = None
    end_time: float | None = None
    title: str = Field(default="", max_length=300)
    headline: str = Field(default="", max_length=300)
    aspect_ratio: str = Field(default="9:16", max_length=12)
    preset: str = Field(default="generic_vertical", max_length=40)
    caption_preset: str | None = Field(default=None, max_length=40)
    auto_framing: bool = True

    @field_validator("end_time")
    @classmethod
    def _positive(cls, value: float | None) -> float | None:
        return value


class ClipUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=300)
    headline: str | None = Field(default=None, max_length=300)
    headline_position: str | None = Field(default=None, pattern="^(top|bottom)$")
    headline_style: dict[str, Any] | None = None
    start_time: float | None = None
    end_time: float | None = None
    aspect_ratio: str | None = Field(default=None, max_length=12)
    crop: dict[str, Any] | None = None
    background: dict[str, Any] | None = None
    caption_config: dict[str, Any] | None = None
    caption_overrides: dict[str, Any] | None = None
    audio_config: dict[str, Any] | None = None
    caption_style_id: str | None = None
    status: str | None = None
    is_favorite: bool | None = None
    notes: str | None = Field(default=None, max_length=2000)


class ClipCropUpdate(BaseModel):
    """Manual crop override from the editor."""

    mode: str = Field(default="manual", pattern="^(auto|center|manual|track)$")
    focus_x: float = Field(default=0.5, ge=0.0, le=1.0)
    focus_y: float = Field(default=0.5, ge=0.0, le=1.0)
    zoom: float = Field(default=1.0, ge=1.0, le=3.0)


class ClipOut(ORMModel):
    id: str
    project_id: str
    video_id: str
    candidate_id: str | None = None
    title: str
    headline: str
    headline_position: str = "top"
    headline_style: dict[str, Any] = Field(default_factory=dict)
    headline_variants: list[str] = Field(default_factory=list)
    start_time: float
    end_time: float
    duration: float
    aspect_ratio: str
    crop: dict[str, Any] = Field(default_factory=dict)
    background: dict[str, Any] = Field(default_factory=dict)
    caption_config: dict[str, Any] = Field(default_factory=dict)
    caption_overrides: dict[str, Any] = Field(default_factory=dict)
    audio_config: dict[str, Any] = Field(default_factory=dict)
    caption_style_id: str | None = None
    status: str
    source: str = "candidate"
    is_favorite: bool = False
    render_count: int = 0
    last_rendered_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class ClipDetail(ClipOut):
    video: dict[str, Any] = Field(default_factory=dict)
    candidate: CandidateOut | None = None
    latest_render: "RenderOut | None" = None


class ClipDuplicateRequest(BaseModel):
    title: str | None = Field(default=None, max_length=300)


# ----------------------------------------------------------------- captions ---
class CaptionWord(BaseModel):
    index: int
    word: str
    start: float
    end: float
    speaker: str = ""
    is_edited: bool = False


class CaptionCue(BaseModel):
    start: float
    end: float
    text: str
    word_count: int
    line_count: int
    words: list[CaptionWord] = Field(default_factory=list)


class CaptionResponse(BaseModel):
    clip_id: str
    enabled: bool
    style: dict[str, Any]
    style_name: str = ""
    cues: list[CaptionCue] = Field(default_factory=list)
    words: list[CaptionWord] = Field(default_factory=list)
    transcript_available: bool = True
    message: str = ""


class CaptionUpdateRequest(BaseModel):
    enabled: bool | None = None
    style: dict[str, Any] | None = None
    style_id: str | None = None
    word_edits: dict[str, str] | None = Field(
        default=None, description="word index -> replacement text (timing is preserved)"
    )


class CaptionStyleOut(ORMModel):
    id: str
    name: str
    preset_key: str
    description: str
    is_system: bool
    is_favorite: bool
    config: dict[str, Any]
    user_id: str | None = None


class CaptionStyleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    preset_key: str = Field(default="", max_length=40)
    description: str = Field(default="", max_length=255)
    config: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------- headlines ---
class HeadlineGenerateRequest(BaseModel):
    count: int = Field(default=5, ge=1, le=10)
    tone: str = Field(default="default", max_length=40)


class HeadlineSuggestion(BaseModel):
    text: str
    source: str  # llm | extractive
    provider: str = ""


class HeadlineResponse(BaseModel):
    clip_id: str
    current: str
    suggestions: list[HeadlineSuggestion] = Field(default_factory=list)
    provider: str = ""
    message: str = ""


class HeadlineSelectRequest(BaseModel):
    text: str = Field(min_length=1, max_length=300)


# ----------------------------------------------------------------- renders ---
class RenderRequest(BaseModel):
    preset: str = Field(default="generic_vertical", max_length=40)
    aspect_ratio: str | None = Field(default=None, max_length=12)
    fps: int | None = Field(default=None, ge=24, le=60)
    priority: int = Field(default=50, ge=0, le=200)


class RenderOut(ORMModel):
    id: str
    clip_id: str
    project_id: str
    job_id: str | None = None
    preset: str
    aspect_ratio: str
    width: int
    height: int
    fps: int
    video_codec: str
    audio_codec: str
    bitrate: str
    status: str
    progress: float
    stage: str
    message: str = ""
    storage_key: str = ""
    thumbnail_key: str = ""
    output_size_bytes: int = 0
    output_duration: float = 0.0
    warnings: list[str] = Field(default_factory=list)
    error_code: str = ""
    error_message: str = ""
    attempts: int = 0
    spec: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_seconds: float = 0.0


class RenderPresetOut(BaseModel):
    key: str
    label: str
    width: int
    height: int
    aspect_ratio: str
    fps: int
    bitrate: str
    max_seconds: int


# ----------------------------------------------------------------- exports ---
class ExportOut(ORMModel):
    id: str
    render_id: str
    clip_id: str
    project_id: str
    platform: str
    filename: str
    storage_key: str
    thumbnail_key: str = ""
    size_bytes: int
    duration_seconds: float
    width: int
    height: int
    download_count: int = 0
    created_at: datetime
    last_downloaded_at: datetime | None = None


class ExportWithClip(ExportOut):
    clip_title: str = ""
    clip_headline: str = ""


ClipDetail.model_rebuild()
