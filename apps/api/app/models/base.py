"""Declarative base and shared column mixins."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column

# JSON that becomes JSONB on PostgreSQL and stays TEXT-backed JSON on SQLite.
JSONColumn = JSON().with_variant(JSONB(), "postgresql")


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map = {dict: JSONColumn}


class UUIDMixin:
    @declared_attr
    def id(cls) -> Mapped[str]:  # noqa: N805
        return mapped_column(String(32), primary_key=True, default=new_id)


class TimestampMixin:
    @declared_attr
    def created_at(cls) -> Mapped[datetime]:  # noqa: N805
        return mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    @declared_attr
    def updated_at(cls) -> Mapped[datetime]:  # noqa: N805
        return mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class UserOwnedMixin:
    @declared_attr
    def user_id(cls) -> Mapped[str]:  # noqa: N805
        return mapped_column(String(32), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
