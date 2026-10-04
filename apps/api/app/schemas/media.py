"""Project, video, upload, transcript and job schemas."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas.common import ORMModel


# ---------------------------------------------------------------- projects ---
class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=2000)
    target_aspect_ratio: str = Field(default="9:16", max_length=12)
    privacy_mode: str = Field(default="cloud", pattern="^(cloud|private_ai)$")
    settings: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _clean(cls, value: str) -> str:
        return value.strip()


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=2000)
    target_aspect_ratio: str | None = Field(default=None, max_length=12)
    status: str | None = None
    settings: dict[str, Any] | None = None
    caption_style_id: str | None = None


class ProjectOut(ORMModel):
    id: str
    name: str
    description: str
    status: str
    target_aspect_ratio: str
    privacy_mode: str
    source_language: str = ""
    settings: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    caption_style_id: str | None = None


class ProjectSummary(ProjectOut):
    video_count: int = 0
    clip_count: int = 0
    candidate_count: int = 0
    render_count: int = 0
    total_duration_seconds: float = 0.0
    latest_video: "VideoOut | None" = None
    active_jobs: list["JobOut"] = Field(default_factory=list)
    progress: float = 0.0
    stage: str = ""


# ------------------------------------------------------------------ videos ---
class VideoOut(ORMModel):
    """Video metadata.

    The narration script itself is deliberately excluded: it can be long, and
    every list endpoint returns this model. `has_narration_script` is enough for
    the UI to show its state; the text is fetched on demand from
    `GET /videos/{id}/narration-script`.
    """

    id: str
    project_id: str
    original_filename: str
    content_type: str
    size_bytes: int
    status: str
    duration_seconds: float
    width: int
    height: int
    fps: float
    video_codec: str
    audio_codec: str
    has_audio: bool
    thumbnail_key: str = ""
    storage_key: str = ""
    error_message: str = ""
    created_at: datetime
    probe: dict[str, Any] = Field(default_factory=dict)
    has_narration_script: bool = False

    @model_validator(mode="before")
    @classmethod
    def _derive_script_flag(cls, data: Any) -> Any:
        """Populate the flag from the ORM object without leaking the script text."""
        script = getattr(data, "narration_script", None)
        if script is None and isinstance(data, dict):
            script = data.get("narration_script")
        if script is None:
            return data
        flag = bool(str(script).strip())
        if isinstance(data, dict):
            return {**data, "has_narration_script": flag}
        # An ORM instance: hand Pydantic a mapping rather than mutating the model.
        return {**{k: getattr(data, k) for k in VideoOut.model_fields if k != "has_narration_script"}, "has_narration_script": flag}


class NarrationScriptOut(BaseModel):
    """The stored script, fetched only when the editor asks for it."""

    video_id: str
    narration_script: str
    has_narration_script: bool
    character_count: int
    word_count: int


class VideoUpdate(BaseModel):
    original_filename: str | None = Field(default=None, max_length=400)
    # Optional narration script. When present, transcription aligns the script to
    # the audio (real DTW word timings) instead of requiring ASR model weights.
    narration_script: str | None = Field(default=None, max_length=200_000)


class UploadInitRequest(BaseModel):
    project_id: str | None = None
    filename: str = Field(min_length=1, max_length=400)
    size_bytes: int = Field(gt=0)
    content_type: str = Field(default="", max_length=120)
    chunk_size: int | None = Field(default=None, ge=256 * 1024, le=64 * 1024 * 1024)
    project_name: str | None = Field(default=None, max_length=160)


class UploadInitResponse(BaseModel):
    upload_id: str
    project_id: str
    video_id: str
    chunk_size: int
    total_chunks: int
    received_chunks: list[int]
    expires_at: datetime | None = None
    storage_backend: str


class UploadStatusResponse(BaseModel):
    upload_id: str
    video_id: str
    project_id: str
    filename: str
    status: str
    size_bytes: int
    received_bytes: int
    received_chunks: list[int]
    total_chunks: int
    chunk_size: int
    progress: float


class UploadCompleteResponse(BaseModel):
    video: VideoOut
    job: "JobOut"
    message: str = "Upload complete. Processing has started."


class SignedUrlOut(BaseModel):
    url: str
    expires_in: int
    kind: str = "download"


# ------------------------------------------------------------- transcripts ---
class TranscriptWordOut(ORMModel):
    id: str
    index: int
    word: str
    start_time: float
    end_time: float
    confidence: float
    speaker: str = ""
    is_edited: bool = False


class TranscriptSegmentOut(ORMModel):
    id: str
    index: int
    start_time: float
    end_time: float
    text: str
    speaker: str = ""
    avg_confidence: float
    word_count: int
    energy: float = 0.0
    energy_variance: float = 0.0
    pause_before: float = 0.0
    speech_rate: float = 0.0


class TranscriptOut(ORMModel):
    id: str
    video_id: str
    project_id: str
    status: str
    provider: str
    model: str
    language: str
    language_probability: float
    duration_seconds: float
    segment_count: int
    word_count: int
    speaker_count: int
    avg_confidence: float
    full_text: str
    error_message: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class TranscriptDetail(TranscriptOut):
    segments: list[TranscriptSegmentOut] = Field(default_factory=list)
    words: list[TranscriptWordOut] = Field(default_factory=list)


class TranscriptWindow(BaseModel):
    """Words inside a time window - what the caption renderer consumes."""

    transcript_id: str
    start: float
    end: float
    words: list[TranscriptWordOut] = Field(default_factory=list)
    text: str = ""


class WordEdit(BaseModel):
    words: dict[str, str] = Field(default_factory=dict, description="word id or index -> new text")


# -------------------------------------------------------------------- jobs ---
class JobOut(ORMModel):
    id: str
    type: str
    status: str
    stage: str
    progress: float
    message: str
    project_id: str | None = None
    video_id: str | None = None
    clip_id: str | None = None
    attempts: int = 0
    max_attempts: int = 3
    error_code: str = ""
    error_message: str = ""
    result: dict[str, Any] = Field(default_factory=dict)
    payload: dict[str, Any] = Field(default_factory=dict)
    cancel_requested: bool = False
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_seconds: float = 0.0


ProjectSummary.model_rebuild()
UploadCompleteResponse.model_rebuild()
