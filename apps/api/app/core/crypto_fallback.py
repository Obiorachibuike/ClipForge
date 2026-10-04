"""Credential encryption without the `cryptography` dependency.

Used only when `cryptography` is not importable. Implements an encrypt-then-MAC
construction: derived keystream (SHA-256 counter mode) XORed with the plaintext,
followed by HMAC-SHA256 over nonce||ciphertext.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    blocks = bytearray()
    counter = 0
    while len(blocks) < length:
        blocks += hashlib.sha256(key + nonce + counter.to_bytes(8, "big")).digest()
        counter += 1
    return bytes(blocks[:length])


def _mac(key: bytes, nonce: bytes, ciphertext: bytes) -> bytes:
    return hmac.new(hashlib.sha256(key + b"mac").digest(), nonce + ciphertext, hashlib.sha256).digest()


def encrypt(key: bytes, plaintext: str) -> str:
    nonce = os.urandom(16)
    data = plaintext.encode("utf-8")
    ciphertext = bytes(a ^ b for a, b in zip(data, _keystream(key, nonce, len(data))))
    tag = _mac(key, nonce, ciphertext)
    return base64.urlsafe_b64encode(nonce + tag + ciphertext).decode("ascii")


def decrypt(key: bytes, payload: str) -> str:
    raw = base64.urlsafe_b64decode(payload.encode("ascii"))
    nonce, tag, ciphertext = raw[:16], raw[16:48], raw[48:]
    if not hmac.compare_digest(tag, _mac(key, nonce, ciphertext)):
        raise ValueError("Credential integrity check failed.")
    data = bytes(a ^ b for a, b in zip(ciphertext, _keystream(key, nonce, len(ciphertext))))
    return data.decode("utf-8")
