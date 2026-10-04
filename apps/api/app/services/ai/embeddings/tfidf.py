"""TF-IDF embeddings.

A real (classic IR) vector space over the transcript corpus. It needs no model
download, is deterministic, and gives the discovery engine genuine lexical
cohesion and novelty signals. It is labelled as a lexical method everywhere it
surfaces; it is not presented as a neural semantic embedding.
"""
from __future__ import annotations

import math
import re
from collections import Counter

from app.services.ai.types import ProviderInfo

TOKEN_RE = re.compile(r"[a-zA-Z][a-zA-Z'\-]+")
STOPWORDS = frozenset(
    """a an and are as at be but by for from had has have he her his i if in into is it its
    just like me my no not of on or our so that the their them then there these they this to
    too up us was we were what when which who will with you your do does did don t s re ve ll
    very really gonna gotta okay yeah know think going get got one two three going's""".split()
)


def tokenize(text: str) -> list[str]:
    return [token.lower() for token in TOKEN_RE.findall(text or "") if token.lower() not in STOPWORDS]


class TfidfEmbeddings:
    name = "tfidf"

    def __init__(self, max_features: int = 4000) -> None:
        self.max_features = max_features
        self.vocabulary: dict[str, int] = {}
        self.idf: list[float] = []
        self.dimensions = 0
        self._fitted = False

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            name=self.name,
            kind="embedding",
            available=True,
            model=f"tfidf-{self.dimensions}d" if self._fitted else "tfidf",
            detail="lexical (TF-IDF) similarity - no model download required",
            is_local=True,
        )

    # ---------------------------------------------------------------- fit ---
    def fit(self, corpus: list[str]) -> TfidfEmbeddings:
        documents = [tokenize(text) for text in corpus if text and text.strip()]
        if not documents:
            self.vocabulary, self.idf, self.dimensions, self._fitted = {}, [], 0, True
            return self
        document_frequency: Counter[str] = Counter()
        term_frequency: Counter[str] = Counter()
        for tokens in documents:
            document_frequency.update(set(tokens))
            term_frequency.update(tokens)
        # Keep the most discriminative terms: high total frequency but not stopword-like.
        ranked = sorted(term_frequency.items(), key=lambda kv: (-kv[1], kv[0]))[: self.max_features]
        self.vocabulary = {term: index for index, (term, _) in enumerate(ranked)}
        self.dimensions = len(self.vocabulary)
        total_docs = len(documents)
        self.idf = [0.0] * self.dimensions
        for term, index in self.vocabulary.items():
            df = document_frequency.get(term, 0)
            self.idf[index] = math.log((1 + total_docs) / (1 + df)) + 1.0
        self._fitted = True
        return self

    # -------------------------------------------------------------- embed ---
    def embed(self, texts: list[str]) -> list[list[float]]:
        if not self._fitted:
            self.fit(texts)
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * max(1, self.dimensions)
        if not self.dimensions:
            return vector
        tokens = tokenize(text)
        if not tokens:
            return vector
        counts = Counter(tokens)
        length = len(tokens)
        for term, count in counts.items():
            index = self.vocabulary.get(term)
            if index is None:
                continue
            tf = count / length
            vector[index] = tf * self.idf[index]
        norm = math.sqrt(sum(value * value for value in vector))
        if norm > 0:
            vector = [value / norm for value in vector]
        return vector

    @staticmethod
    def similarity(a: list[float], b: list[float]) -> float:
        if not a or not b:
            return 0.0
        return float(sum(x * y for x, y in zip(a, b)))  # vectors are already L2-normalised

    def term_weights(self, text: str, top: int = 8) -> list[tuple[str, float]]:
        vector = self._vector(text)
        inverse = {index: term for term, index in self.vocabulary.items()}
        pairs = [(inverse[i], value) for i, value in enumerate(vector) if value > 0]
        pairs.sort(key=lambda kv: -kv[1])
        return pairs[:top]

    def novelty(self, text: str, corpus_vectors: list[list[float]]) -> float:
        """1 - max similarity to the corpus (how new this text is)."""
        vector = self._vector(text)
        if not corpus_vectors:
            return 1.0
        best = max(self.similarity(vector, other) for other in corpus_vectors)
        return max(0.0, 1.0 - best)

    def coherence(self, text: str) -> float:
        """Mean pairwise similarity of consecutive sentences: how much the text
        stays on one topic instead of drifting."""
        sentences = [s for s in re.split(r"[.!?]+", text or "") if len(s.strip()) > 12]
        if len(sentences) < 2:
            return 0.6
        vectors = [self._vector(s) for s in sentences]
        scores = [self.similarity(vectors[i], vectors[i + 1]) for i in range(len(vectors) - 1)]
        return float(sum(scores) / len(scores)) if scores else 0.6
