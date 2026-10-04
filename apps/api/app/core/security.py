"""Authentication primitives: password hashing, signed session tokens, CSRF,
and encryption-at-rest for stored provider credentials."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
import jwt

from app.core.config import settings
from app.core.errors import AuthError, ValidationError

_BCRYPT_ROUNDS = 12
_BCRYPT_MAX_BYTES = 72


# --------------------------------------------------------------- passwords ---
def hash_password(password: str) -> str:
    if not password:
        raise ValidationError("Password must not be empty.")
    raw = password.encode("utf-8")[:_BCRYPT_MAX_BYTES]
    return bcrypt.hashpw(raw, bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)).decode("ascii")


def verify_password(password: str, hashed: str) -> bool:
    if not password or not hashed:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8")[:_BCRYPT_MAX_BYTES], hashed.encode("ascii"))
    except (ValueError, TypeError):
        return False


def password_strength_error(password: str) -> str | None:
    if len(password) < settings.password_min_length:
        return f"Password must be at least {settings.password_min_length} characters."
    if password.lower() in {"password", "12345678", "qwertyui", "letmein1"}:
        return "That password is too common."
    if not any(c.isalpha() for c in password) or not any(c.isdigit() for c in password):
        return "Password must contain at least one letter and one number."
    return None


# ------------------------------------------------------------------ tokens ---
def create_session_token(user_id: str, session_id: str, expires_at: datetime | None = None) -> str:
    now = datetime.now(UTC)
    # `expires_at` may come straight off an ORM object, where SQLite hands back a
    # naive datetime; `.timestamp()` would then read it as local time.
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    payload: dict[str, Any] = {
        "sub": user_id,
        "sid": session_id,
        "iat": int(now.timestamp()),
        "exp": int((expires_at or (now + timedelta(minutes=settings.access_token_ttl_minutes))).timestamp()),
        "typ": "session",
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_session_token(token: str) -> dict[str, Any]:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("Your session has expired. Please sign in again.", code="session_expired") from exc
    except jwt.PyJWTError as exc:
        raise AuthError("Your session is invalid. Please sign in again.", code="invalid_session") from exc
    if payload.get("typ") != "session":
        raise AuthError("Your session is invalid. Please sign in again.", code="invalid_session")
    return payload


def token_fingerprint(token: str) -> str:
    """SHA-256 fingerprint used to store sessions without keeping the raw token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def session_expiry() -> datetime:
    return datetime.now(UTC) + timedelta(minutes=settings.access_token_ttl_minutes)


# -------------------------------------------------------------------- CSRF ---
def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_matches(cookie_token: str | None, header_token: str | None) -> bool:
    if not cookie_token or not header_token:
        return False
    return hmac.compare_digest(cookie_token, header_token)


def new_reset_token() -> str:
    return secrets.token_urlsafe(48)


# ------------------------------------------------- credential encryption ----
def _fernet_key() -> bytes:
    material = settings.encryption_key or settings.secret_key or settings.jwt_secret
    digest = hashlib.sha256(material.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def encrypt_secret(plaintext: str) -> str:
    """Encrypt an API key for storage. Falls back to an authenticated AES-GCM
    envelope when `cryptography` is unavailable."""
    if not plaintext:
        return ""
    try:
        from cryptography.fernet import Fernet

        return "fernet:" + Fernet(_fernet_key()).encrypt(plaintext.encode("utf-8")).decode("ascii")
    except Exception:  # pragma: no cover - optional dependency
        from app.core.crypto_fallback import encrypt as fallback_encrypt

        key = hashlib.sha256((settings.encryption_key or settings.secret_key).encode()).digest()
        return "hmac:" + fallback_encrypt(key, plaintext)


def decrypt_secret(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    if ciphertext.startswith("fernet:"):
        from cryptography.fernet import Fernet

        return Fernet(_fernet_key()).decrypt(ciphertext[len("fernet:") :].encode("ascii")).decode("utf-8")
    if ciphertext.startswith("hmac:"):
        from app.core.crypto_fallback import decrypt as fallback_decrypt

        key = hashlib.sha256((settings.encryption_key or settings.secret_key).encode()).digest()
        return fallback_decrypt(key, ciphertext[len("hmac:") :])
    return ciphertext


def mask_secret(plaintext: str) -> str:
    if not plaintext:
        return ""
    if len(plaintext) <= 8:
        return "•" * len(plaintext)
    return f"{plaintext[:4]}{'•' * 6}{plaintext[-4:]}"
