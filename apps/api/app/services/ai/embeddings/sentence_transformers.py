"""Optional neural embeddings via sentence-transformers.

Used when the package and a local model are available; otherwise the registry
falls back to TF-IDF. Loading is lazy and cached, and a failure to load is
reported (never silently simulated).
"""
from __future__ import annotations

import threading
from typing import Any

from app.core.errors import ProviderError
from app.core.logging import get_logger
from app.services.ai.types import ProviderInfo

log = get_logger(__name__)

_lock = threading.Lock()
_model: Any = None
DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


def package_available() -> bool:
    try:
        import sentence_transformers  # noqa: F401

        return True
    except Exception:
        return False


def model_cached(name: str = DEFAULT_MODEL) -> bool:
    from pathlib import Path

    needle = name.split("/")[-1].lower()
    for root in (Path.home() / ".cache" / "torch" / "sentence_transformers", Path.home() / ".cache" / "huggingface"):
        if root.exists():
            try:
                for entry in root.iterdir():
                    if needle in entry.name.lower():
                        return True
            except OSError:
                continue
    return False


class SentenceTransformerEmbeddings:
    name = "sentence-transformers"

    def __init__(self, model_name: str = DEFAULT_MODEL) -> None:
        self.model_name = model_name
        self.dimensions = 384

    def info(self) -> ProviderInfo:
        available = package_available()
        return ProviderInfo(
            name=self.name,
            kind="embedding",
            available=available and model_cached(self.model_name),
            model=self.model_name,
            detail="" if available else "sentence-transformers not installed",
            is_local=True,
        )

    def _load(self):
        global _model
        if not package_available():
            raise ProviderError("sentence-transformers", "package not installed", retryable=False)
        with _lock:
            if _model is None:
                from sentence_transformers import SentenceTransformer

                _model = SentenceTransformer(self.model_name)
                self.dimensions = int(_model.get_sentence_embedding_dimension() or 384)
        return _model

    def embed(self, texts: list[str]) -> list[list[float]]:
        model = self._load()
        vectors = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        return [list(map(float, vector)) for vector in vectors]

    @staticmethod
    def similarity(a: list[float], b: list[float]) -> float:
        if not a or not b:
            return 0.0
        return float(sum(x * y for x, y in zip(a, b)))
