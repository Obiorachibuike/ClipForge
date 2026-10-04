"""Authentication and account schemas."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.core.security import password_strength_error
from app.schemas.common import ORMModel


class Credentials(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6, max_length=200)


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6, max_length=200)
    name: str = Field(default="", max_length=120)

    @field_validator("password")
    @classmethod
    def _strong_enough(cls, value: str) -> str:
        error = password_strength_error(value)
        if error:
            raise ValueError(error)
        return value

    @field_validator("name")
    @classmethod
    def _clean_name(cls, value: str) -> str:
        return (value or "").strip()[:120]


class UserOut(ORMModel):
    id: str
    email: str
    name: str
    avatar_url: str = ""
    plan: str
    privacy_mode: str
    default_aspect_ratio: str = "9:16"
    default_caption_preset: str = "bold"
    is_admin: bool = False
    created_at: datetime
    last_login_at: datetime | None = None


class SessionOut(BaseModel):
    user: UserOut
    csrf_token: str
    expires_at: datetime


class PasswordResetRequest(BaseModel):
    email: EmailStr


class PasswordResetConfirm(BaseModel):
    token: str = Field(min_length=10, max_length=400)
    password: str = Field(min_length=6, max_length=200)

    @field_validator("password")
    @classmethod
    def _strong_enough(cls, value: str) -> str:
        error = password_strength_error(value)
        if error:
            raise ValueError(error)
        return value


class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(min_length=6, max_length=200)

    @field_validator("new_password")
    @classmethod
    def _strong_enough(cls, value: str) -> str:
        error = password_strength_error(value)
        if error:
            raise ValueError(error)
        return value


class UserUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    privacy_mode: str | None = None
    default_aspect_ratio: str | None = None
    default_caption_preset: str | None = None
    preferences: dict | None = None


class AIProviderCreate(BaseModel):
    kind: str = Field(pattern="^(llm|transcription|embedding)$")
    provider: str = Field(min_length=2, max_length=64)
    label: str = Field(default="", max_length=120)
    model: str = Field(default="", max_length=160)
    base_url: str = Field(default="", max_length=400)
    api_key: str = Field(default="", max_length=400)
    is_default: bool = True


class AIProviderOut(ORMModel):
    id: str
    kind: str
    provider: str
    label: str
    model: str
    base_url: str
    is_default: bool
    is_enabled: bool
    has_api_key: bool = False
    masked_api_key: str = ""
    created_at: datetime


class CapabilityOut(BaseModel):
    """What this deployment can actually do, reported truthfully."""

    ffmpeg: bool
    ffprobe: bool
    media_pipeline: bool
    vision: dict
    transcription: dict
    llm: dict
    embeddings: dict
    storage_backend: str
    queue_backend: str
    billing: list[dict]
    processor: dict
    environment: str
