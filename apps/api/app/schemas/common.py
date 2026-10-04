"""Shared response envelopes and pagination."""
from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int
    has_more: bool = False

    @classmethod
    def build(cls, items: list[T], total: int, limit: int, offset: int) -> "Page[T]":
        return cls(items=items, total=total, limit=limit, offset=offset, has_more=offset + len(items) < total)


class ErrorResponse(BaseModel):
    """Uniform error shape - the frontend renders `message`, logs `code`."""

    code: str = Field(examples=["not_found"])
    message: str = Field(examples=["Project not found."])
    details: dict[str, Any] = Field(default_factory=dict)
    request_id: str = ""


class OkResponse(BaseModel):
    ok: bool = True
    message: str = ""


class MessageResponse(BaseModel):
    message: str
    level: str = "info"
