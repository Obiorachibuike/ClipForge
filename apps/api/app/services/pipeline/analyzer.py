"""The clip discovery engine.

Pipeline:
    words -> sentences -> scored windows -> non-overlapping selection -> copy

Every score is computed from measured data - transcript text, word timings, ASR
confidence, decoded audio energy, pause structure and TF-IDF/embedding
similarity. When an LLM provider is configured its assessment is blended in and
recorded; when it is not, scores are purely structural and the UI says so.
Nothing here invents a timestamp, a quote or a number.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from app.core.logging import get_logger
from app.services.ai.types import MomentAnalysis, WordTiming
from app.services.pipeline.lexicons import (
    BACK_REFERENCE_PHRASES,
    CATEGORY_MARKERS,
    CONTEXT_DEPENDENT_OPENERS,
    CONTRAST_MARKERS,
    EMOTION_WORDS,
    FILLER_WORDS,
    HOOK_LEAD_WORDS,
    HOOK_PHRASES,
    HUMOR_WORDS,
    INSIGHT_PHRASES,
    LESSON_MARKERS,
    NUMBER_RE,
    SENTENCE_END_RE,
    STATISTIC_RE,
    STORY_PHRASES,
    SURPRISE_PHRASES,
    contains_any,
    word_set,
)

log = get_logger(__name__)

# ------------------------------------------------------------------ config ---
# Component weights sum to exactly 1.0. Kept in one place so the scoring model
# is auditable and can be tuned without hunting through the codebase.
DEFAULT_WEIGHTS: dict[str, float] = {
    "hook": 0.18,
    "self_contained": 0.16,
    "insight": 0.12,
    "surprise": 0.10,
    "emotion": 0.10,
    "dynamics": 0.08,
    "novelty": 0.08,
    "length": 0.08,
    "story": 0.06,
    "confidence": 0.04,
}
LLM_BLEND = 0.40  # share of the final score taken from the LLM when present

MIN_CLIP_SECONDS = 10.0
MAX_CLIP_SECONDS = 95.0
IDEAL_CLIP_SECONDS = 38.0
MAX_OVERLAP_RATIO = 0.35

PAUSE_BREAK_SECONDS = 0.45
SPEAKER_BREAK_GAP = 0.35


@dataclass
class Sentence:
    text: str
    start: float
    end: float
    words: list[WordTiming]
    speaker: str = ""
    energy: float = 0.0
    energy_variance: float = 0.0
    pause_before: float = 0.0
    pause_after: float = 0.0
    confidence: float = 0.0
    clean_text: str = ""
    tokens: set[str] = field(default_factory=set)

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def word_count(self) -> int:
        return len(self.words)

    @property
    def speech_rate(self) -> float:
        return self.word_count / self.duration if self.duration > 0 else 0.0


@dataclass
class Candidate:
    start: float
    end: float
    sentences: list[Sentence]
    transcript: str
    score: int
    scores: dict[str, float]
    reason: str
    title: str
    hook: str
    category: str
    keywords: list[str]
    speaker: str
    analyzer: str
    llm: MomentAnalysis | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def to_payload(self, index: int = 0) -> dict[str, Any]:
        return {
            "start_time": round(self.start, 3),
            "end_time": round(self.end, 3),
            "duration": round(self.duration, 3),
            "title": self.title,
            "hook": self.hook,
            "reason": self.reason,
            "score": self.score,
            "scores": {k: round(v, 1) for k, v in self.scores.items()},
            "transcript": self.transcript,
            "speaker": self.speaker,
            "category": self.category,
            "keywords": self.keywords,
            "analyzer": self.analyzer,
            "rank": index,
            "meta": self.meta,
        }


# --------------------------------------------------------------- sentences ---
def build_sentences(
    words: Sequence[WordTiming],
    *,
    energy_curve: tuple[list[float], list[float]] | None = None,
    max_sentence_seconds: float = 18.0,
) -> list[Sentence]:
    """Group word timings into sentences using punctuation, pauses and speaker
    changes - the natural boundaries a human editor would cut on."""
    if not words:
        return []
    ordered = sorted(words, key=lambda w: (w.start, w.end))
    groups: list[list[WordTiming]] = []
    current: list[WordTiming] = []
    for word in ordered:
        if current:
            gap = word.start - current[-1].end
            speaker_change = bool(word.speaker and current[-1].speaker and word.speaker != current[-1].speaker)
            previous_text = current[-1].word.strip()
            long_enough = (current[-1].end - current[0].start) >= max_sentence_seconds
            break_here = (
                (gap >= PAUSE_BREAK_SECONDS and SENTENCE_END_RE.search(previous_text) is not None)
                or gap >= 1.2
                or (speaker_change and gap >= SPEAKER_BREAK_GAP)
                or long_enough
            )
            if break_here:
                groups.append(current)
                current = []
        current.append(word)
        if SENTENCE_END_RE.search(word.word.strip()) and (word.end - current[0].start) >= 1.0:
            groups.append(current)
            current = []
    if current:
        groups.append(current)

    sentences: list[Sentence] = []
    for index, group in enumerate(groups):
        text = " ".join(w.word.strip() for w in group).strip()
        if not text:
            continue
        clean = _clean_text(text)
        sentence = Sentence(
            text=text,
            start=group[0].start,
            end=group[-1].end,
            words=list(group),
            speaker=group[0].speaker or "",
            confidence=_mean([w.confidence for w in group]) if any(w.confidence for w in group) else 0.0,
            clean_text=clean,
            tokens=word_set(clean),
        )
        sentences.append(sentence)

    # Pauses + acoustic features relative to neighbours.
    for index, sentence in enumerate(sentences):
        previous = sentences[index - 1] if index > 0 else None
        following = sentences[index + 1] if index + 1 < len(sentences) else None
        sentence.pause_before = max(0.0, sentence.start - previous.end) if previous else 0.0
        sentence.pause_after = max(0.0, following.start - sentence.end) if following else 0.0
        if energy_curve:
            times, rms = energy_curve
            window = _slice_curve(times, rms, sentence.start, sentence.end)
            if window:
                sentence.energy = _mean(window)
                sentence.energy_variance = _std(window)
    all_energy = [s.energy for s in sentences if s.energy > 0]
    baseline = _median(all_energy) if all_energy else 0.0
    for sentence in sentences:
        sentence.energy = sentence.energy / baseline if baseline > 0 else 1.0
    return sentences


def _slice_curve(times: list[float], values: list[float], start: float, end: float) -> list[float]:
    if not times:
        return []
    start_index = _bisect(times, start)
    end_index = _bisect(times, end)
    return values[start_index : max(start_index + 1, end_index)]


def _bisect(values: list[float], target: float) -> int:
    low, high = 0, len(values)
    while low < high:
        mid = (low + high) // 2
        if values[mid] < target:
            low = mid + 1
        else:
            high = mid
    return low


# ---------------------------------------------------------------- scoring ---
def score_window(
    window: Sequence[Sentence],
    *,
    weights: dict[str, float] | None = None,
    corpus_idf: dict[str, float] | None = None,
    similarity_fn=None,
    all_sentences: Sequence[Sentence] | None = None,
    existing_vectors: list[list[float]] | None = None,
    embed_fn=None,
) -> tuple[dict[str, float], str]:
    """Return per-signal scores (0-100) plus a human explanation of the top
    contributors. Signals are measured, never guessed."""
    weights = weights or DEFAULT_WEIGHTS
    duration = window[-1].end - window[0].start
    text = " ".join(s.clean_text for s in window)
    lowered = f" {text.lower()} "
    first = window[0]
    last = window[-1]

    # ---------------------------------------------------------------- hook ---
    hook_hits = contains_any(first.clean_text, HOOK_PHRASES)
    lead_bonus = 12.0 if first.clean_text.split(" ")[:1] and first.clean_text.split(" ")[0].lower() in HOOK_LEAD_WORDS else 0.0
    question_bonus = 14.0 if "?" in first.text else 0.0
    number_bonus = 10.0 if NUMBER_RE.search(first.clean_text) else 0.0
    second_person = 8.0 if re.search(r"\b(you|your)\b", first.clean_text.lower()) else 0.0
    brevity_bonus = 10.0 if first.word_count <= 14 else -6.0 if first.word_count > 26 else 0.0
    emphasis = min(18.0, max(0.0, (first.energy - 1.0)) * 48.0)
    hook = _clamp(
        22
        + hook_hits * 17
        + lead_bonus
        + question_bonus
        + number_bonus
        + second_person
        + brevity_bonus
        + emphasis
        - (16.0 if _is_context_dependent(first.clean_text) else 0.0)
    )

    # ------------------------------------------------------------ surprise ---
    surprise_hits = contains_any(lowered, SURPRISE_PHRASES)
    contrast = len({token for token in CONTRAST_MARKERS if token in word_set(text)}) * 5.0
    rarity = _rarity(text, corpus_idf)
    statistic = 14.0 if STATISTIC_RE.search(text) else 0.0
    surprise = _clamp(14 + surprise_hits * 22 + contrast + rarity * 46 + statistic)

    # ------------------------------------------------------------- emotion ---
    high_emotion = len(word_set(text) & EMOTION_WORDS["high"])
    medium_emotion = len(word_set(text) & EMOTION_WORDS["medium"])
    exclamations = text.count("!")
    laughter = len(word_set(text) & HUMOR_WORDS)
    variance_signal = _mean([s.energy_variance for s in window]) * 60.0
    emotion = _clamp(12 + high_emotion * 13 + medium_emotion * 7 + exclamations * 8 + laughter * 11 + min(24.0, variance_signal))

    # ------------------------------------------------------------- insight ---
    insight_hits = contains_any(lowered, INSIGHT_PHRASES)
    lesson_words = len(word_set(text) & LESSON_MARKERS)
    declarative = sum(1 for s in window if SENTENCE_END_RE.search(s.text.strip()))
    density = declarative / max(1, len(window))
    payoff = 12.0 if any(marker in lowered for marker in ("therefore", "so that", "which means", "in the end", "the result")) else 0.0
    question_answer = 10.0 if "?" in first.text and any("?" not in s.text for s in window[1:]) else 0.0
    insight = _clamp(
        16 + insight_hits * 9 + lesson_words * 4 + density * 22 + payoff + question_answer
    )

    # --------------------------------------------------------------- story ---
    story_hits = contains_any(lowered, STORY_PHRASES)
    past_tense = sum(1 for hint in ("was ", "were ", "had ", "did ", "went ", "told ") if hint in lowered)
    chronology = 8.0 if re.search(r"\b(then|after that|next|later|finally)\b", lowered) else 0.0
    story = _clamp(10 + story_hits * 16 + past_tense * 5 + chronology)

    # ------------------------------------------------------- self-contained ---
    coherence = 0.6
    if similarity_fn and len(window) > 1 and embed_fn:
        vectors = embed_fn([s.clean_text for s in window])
        pairs = [
            similarity_fn(vectors[i], vectors[i + 1]) for i in range(len(vectors) - 1)
        ]
        coherence = _mean(pairs) if pairs else 0.5
    opener_penalty = 26.0 if _is_context_dependent(first.clean_text) else 0.0
    backref_penalty = 14.0 if any(phrase in lowered for phrase in BACK_REFERENCE_PHRASES) else 0.0
    closes_cleanly = SENTENCE_END_RE.search(last.text.strip()) is not None
    ending_bonus = 16.0 if closes_cleanly else 0.0
    drift_penalty = 0.0
    if all_sentences is not None and len(all_sentences) > 2:
        neighbours = _context_similarity(first, all_sentences, similarity_fn, embed_fn)
        drift_penalty = max(0.0, (neighbours - coherence)) * 40.0
    pause_bonus = 8.0 if first.pause_before >= 0.35 else 0.0
    self_contained = _clamp(
        24 + coherence * 52 + ending_bonus + pause_bonus - opener_penalty - backref_penalty - drift_penalty
    )

    # ------------------------------------------------------------ dynamics ---
    rates = [s.speech_rate for s in window if s.speech_rate > 0]
    rate = _mean(rates) if rates else 0.0
    rate_fit = 100.0 - min(100.0, abs(rate - 2.7) * 28.0)
    rate_variance = _std(rates) if len(rates) > 1 else 0.0
    energy_swing = _std([s.energy for s in window]) if len(window) > 1 else 0.0
    dynamics = _clamp(rate_fit * 0.55 + min(30.0, rate_variance * 45.0) + min(25.0, energy_swing * 30.0) + 12.0)

    # ------------------------------------------------------------ confidence ---
    confidence_values = [s.confidence for s in window if s.confidence > 0]
    if confidence_values:
        confidence = _clamp(_mean(confidence_values) * 100.0)
    else:
        # Providers without confidence values are neutral (never inflated).
        confidence = 65.0

    # --------------------------------------------------------------- length ---
    if duration < MIN_CLIP_SECONDS:
        length = max(8.0, (duration / MIN_CLIP_SECONDS) * 45.0)
    elif duration > MAX_CLIP_SECONDS:
        length = max(10.0, 62.0 - (duration - MAX_CLIP_SECONDS) * 0.9)
    else:
        spread = abs(duration - IDEAL_CLIP_SECONDS)
        length = _clamp(100.0 - (spread / IDEAL_CLIP_SECONDS) * 42.0, 40.0, 100.0)

    # --------------------------------------------------------------- novelty ---
    novelty = 70.0
    if existing_vectors is not None and embed_fn and similarity_fn:
        vector = embed_fn([text])[0]
        if existing_vectors:
            best = max(similarity_fn(vector, other) for other in existing_vectors)
            novelty = _clamp((1.0 - best) * 100.0 + 10.0)
    if any(phrase in lowered for phrase in ("as i said", "like i said", "again, ")):
        novelty -= 12.0

    scores = {
        "hook": _clamp(hook),
        "surprise": _clamp(surprise),
        "emotion": _clamp(emotion),
        "insight": _clamp(insight),
        "story": _clamp(story),
        "self_contained": _clamp(self_contained),
        "dynamics": _clamp(dynamics),
        "confidence": _clamp(confidence),
        "novelty": _clamp(novelty),
        "length": _clamp(length),
    }

    total = sum(scores[key] * weights.get(key, 0.0) for key in scores)
    reason = _explain(scores, window)
    return {**scores, "structural": round(total, 2)}, reason


def _explain(scores: dict[str, float], window: Sequence[Sentence]) -> str:
    """Build the 'reason' text from the strongest measured signals."""
    labels = {
        "hook": "opens with a strong hook",
        "surprise": "contains a genuinely unexpected turn",
        "emotion": "carries emotional intensity",
        "insight": "delivers a clear, useful insight",
        "story": "tells a complete story",
        "self_contained": "stands alone without missing context",
        "dynamics": "has energetic, well-paced delivery",
        "novelty": "covers material not repeated elsewhere in the video",
        "confidence": "transcript confidence is high",
        "length": "length suits short-form platforms",
    }
    ranked = sorted(
        ((key, value) for key, value in scores.items() if key in labels),
        key=lambda kv: -kv[1],
    )
    top = [(key, value) for key, value in ranked[:2] if value >= 55]
    if not top:
        top = ranked[:1]
    parts = [f"{labels[key]} ({value:.0f}/100)" for key, value in top]
    weakest = ranked[-1][0] if ranked and ranked[-1][1] < 45 else None
    text = "Strong standalone moment: " + " and ".join(parts) + "."
    if weakest:
        text += f" Weakest signal: {labels.get(weakest, weakest).replace('opens with ', '')}."
    return text


def _category(scores: dict[str, float], window: Sequence[Sentence]) -> str:
    text = " ".join(s.clean_text for s in window).lower()
    votes: dict[str, float] = {}
    for category, markers in CATEGORY_MARKERS.items():
        hits = sum(1 for marker in markers if marker in text)
        if hits:
            votes[category] = hits * 10.0
    votes["insight"] = votes.get("insight", 0.0) + scores.get("insight", 0) * 0.4
    votes["story"] = votes.get("story", 0.0) + scores.get("story", 0) * 0.35
    votes["emotional"] = votes.get("emotional", 0.0) + scores.get("emotion", 0) * 0.4
    votes["funny"] = votes.get("funny", 0.0) + (scores.get("emotion", 0) * 0.25 if "haha" in text else 0)
    return max(votes.items(), key=lambda kv: kv[1])[0] if votes else "insight"


def _is_context_dependent(text: str) -> bool:
    first_word = re.sub(r"[^\w']", "", text.strip().split(" ")[0].lower()) if text.strip() else ""
    return first_word in CONTEXT_DEPENDENT_OPENERS


def _rarity(text: str, corpus_idf: dict[str, float] | None) -> float:
    if not corpus_idf:
        return 0.35
    tokens = [token for token in word_set(text) if len(token) > 3 and token not in FILLER_WORDS]
    if not tokens:
        return 0.2
    idf_values = sorted((corpus_idf.get(token, 0.0) for token in tokens), reverse=True)[:6]
    maximum = max(corpus_idf.values()) if corpus_idf else 1.0
    return (sum(idf_values) / len(idf_values)) / maximum if maximum > 0 else 0.3


def _clean_text(text: str) -> str:
    clean = re.sub(r"\s+", " ", text).strip()
    clean = re.sub(r"\s([,.!?;:])", r"\1", clean)
    return clean


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, float(value)))


def _mean(values: Sequence[float]) -> float:
    values = [v for v in values]
    return sum(values) / len(values) if values else 0.0


def _std(values: Sequence[float]) -> float:
    values = list(values)
    if len(values) < 2:
        return 0.0
    mean = _mean(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _context_similarity(
    sentence: Sentence,
    all_sentences: Sequence[Sentence],
    similarity_fn=None,
    embed_fn=None,
) -> float:
    """Similarity between this sentence and sentences outside a typical window."""
    if not similarity_fn or not embed_fn:
        return 0.0
    index = next((i for i, s in enumerate(all_sentences) if s.start == sentence.start), None)
    if index is None:
        return 0.0
    neighbours = [
        s for i, s in enumerate(all_sentences) if abs(i - index) > 3 and len(s.clean_text) > 20
    ][:12]
    if not neighbours:
        return 0.0
    vectors = embed_fn([sentence.clean_text] + [n.clean_text for n in neighbours])
    base, others = vectors[0], vectors[1:]
    return max(similarity_fn(base, other) for other in others)


# ------------------------------------------------------------ window search ---
def _make_window(sentences: Sequence[Sentence], start_index: int, end_index: int) -> list[Sentence]:
    return list(sentences[start_index : end_index + 1])


def generate_candidates(
    sentences: Sequence[Sentence],
    *,
    max_candidates: int = 12,
    min_duration: float = MIN_CLIP_SECONDS,
    max_duration: float = MAX_CLIP_SECONDS,
    corpus_idf: dict[str, float] | None = None,
    similarity_fn=None,
    embed_fn=None,
    weights: dict[str, float] | None = None,
    language: str = "en",
) -> list[Candidate]:
    """Search window boundaries and keep the best non-overlapping moments."""
    if not sentences:
        return []
    raw: list[Candidate] = []
    count = len(sentences)
    existing_vectors: list[list[float]] = []

    for start_index in range(count):
        for end_index in range(start_index, count):
            window = _make_window(sentences, start_index, end_index)
            duration = window[-1].end - window[0].start
            if duration < min_duration:
                continue
            if duration > max_duration:
                break
            scores, reason = score_window(
                window,
                weights=weights,
                corpus_idf=corpus_idf,
                similarity_fn=similarity_fn,
                all_sentences=sentences,
                existing_vectors=None,
                embed_fn=embed_fn,
            )
            structural = scores.get("structural", 0.0)
            raw.append(
                Candidate(
                    start=window[0].start,
                    end=window[-1].end,
                    sentences=window,
                    transcript=" ".join(s.clean_text for s in window),
                    score=int(round(structural)),
                    scores=scores,
                    reason=reason,
                    title="",
                    hook="",
                    category=_category(scores, window),
                    keywords=[],
                    speaker=window[0].speaker,
                    analyzer="structural-v1",
                    meta={
                        "sentence_count": len(window),
                        "word_count": sum(s.word_count for s in window),
                        "speech_rate": round(_mean([s.speech_rate for s in window]), 3),
                        "avg_energy": round(_mean([s.energy for s in window]), 3),
                    },
                )
            )

    if not raw:
        return []

    # Pre-rank before the (more expensive) novelty pass.
    raw.sort(key=lambda c: -c.score)
    shortlist = raw[: max(120, max_candidates * 12)]

    if embed_fn and similarity_fn:
        try:
            texts = [c.transcript for c in shortlist]
            vectors = embed_fn(texts)
            for candidate, vector in zip(shortlist, vectors):
                best = max(
                    (similarity_fn(vector, other) for i, other in enumerate(vectors) if shortlist[i] is not candidate),
                    default=0.0,
                )
                novelty = _clamp((1.0 - best) * 100.0 + 10.0)
                candidate.scores["novelty"] = novelty
                structural = sum(
                    candidate.scores.get(key, 0.0) * (weights or DEFAULT_WEIGHTS).get(key, 0.0)
                    for key in DEFAULT_WEIGHTS
                )
                candidate.scores["structural"] = round(structural, 2)
                candidate.score = int(round(structural))
        except Exception as exc:  # embeddings must never break discovery
            log.warning("discovery.embedding_failed", error=str(exc))

    shortlist.sort(key=lambda c: -c.score)

    selected: list[Candidate] = []
    for candidate in shortlist:
        if len(selected) >= max_candidates:
            break
        if any(_overlap_ratio(candidate, kept) > MAX_OVERLAP_RATIO for kept in selected):
            continue
        selected.append(candidate)

    for index, candidate in enumerate(selected):
        candidate.title = extractive_title(candidate)
        candidate.hook = extractive_hook(candidate)
        candidate.keywords = extract_keywords(candidate.transcript, corpus_idf)
        candidate.meta["rank"] = index
    return selected


def _overlap_ratio(a: Candidate, b: Candidate) -> float:
    overlap = max(0.0, min(a.end, b.end) - max(a.start, b.start))
    if overlap <= 0:
        return 0.0
    shortest = min(a.duration, b.duration)
    return overlap / shortest if shortest > 0 else 0.0


# ------------------------------------------------------------------- copy ---
def extractive_title(candidate: Candidate) -> str:
    """Deterministic, transcript-derived title (used when no LLM is configured;
    the LLM path replaces it with authored copy)."""
    first = candidate.sentences[0].clean_text
    text = re.sub(r"^(and|but|so|well|okay|ok|yeah|um|uh)\s+", "", first, flags=re.IGNORECASE).strip()
    text = re.sub(r"\s+", " ", text).strip(" .,!?-")
    if not text:
        text = candidate.transcript[:70]
    words = text.split(" ")
    title = ""
    for word in words:
        if len(title) + len(word) + 1 > 62:
            break
        title = f"{title} {word}".strip()
    title = title.rstrip(" ,;:-")
    if title and title[-1] not in ".!?":
        title = title
    return title[:70]


def extractive_hook(candidate: Candidate) -> str:
    """The hook is the opening sentence as spoken - no rewriting, no invention."""
    sentence = candidate.sentences[0].clean_text.strip()
    words = sentence.split(" ")
    if len(words) <= 24:
        return sentence[:160]
    return " ".join(words[:24]) + "…"


def extract_keywords(text: str, corpus_idf: dict[str, float] | None, limit: int = 6) -> list[str]:
    tokens = [token for token in word_set(text) if len(token) > 3 and token not in FILLER_WORDS]
    if corpus_idf:
        tokens.sort(key=lambda token: -(corpus_idf.get(token, 0.0) * (text.lower().count(token) ** 0.5)))
    else:
        tokens.sort(key=lambda token: -text.lower().count(token))
    result: list[str] = []
    for token in tokens:
        if token not in result:
            result.append(token)
        if len(result) >= limit:
            break
    return result


def build_corpus_idf(sentences: Sequence[Sentence]) -> dict[str, float]:
    """IDF over the transcript's own sentences: rarity is relative to the video,
    which is exactly what makes a phrase feel surprising to this audience."""
    documents = [s.tokens for s in sentences if s.tokens]
    if not documents:
        return {}
    document_frequency: dict[str, int] = {}
    for tokens in documents:
        for token in tokens:
            document_frequency[token] = document_frequency.get(token, 0) + 1
    total = len(documents)
    return {
        token: math.log((1 + total) / (1 + frequency)) + 1.0 for token, frequency in document_frequency.items()
    }


def apply_llm_scores(
    candidates: Sequence[Candidate],
    assessments: Sequence[MomentAnalysis],
    *,
    provider_name: str,
) -> list[Candidate]:
    """Blend LLM assessment into the structural score and record provenance."""
    for candidate, assessment in zip(candidates, assessments):
        if not assessment or not any(
            [assessment.engagement, assessment.hook_strength, assessment.standalone, assessment.clarity]
        ):
            continue
        llm_score = _mean([assessment.engagement, assessment.hook_strength, assessment.standalone, assessment.clarity])
        structural = candidate.scores.get("structural", float(candidate.score))
        blended = structural * (1 - LLM_BLEND) + llm_score * LLM_BLEND
        candidate.scores.update(
            {
                "llm_engagement": round(assessment.engagement, 1),
                "llm_hook": round(assessment.hook_strength, 1),
                "llm_standalone": round(assessment.standalone, 1),
                "llm_clarity": round(assessment.clarity, 1),
                "llm_blend": round(llm_score, 2),
                "structural": round(structural, 2),
                "final": round(blended, 2),
            }
        )
        candidate.score = int(round(blended))
        candidate.llm = assessment
        candidate.analyzer = f"structural-v1+{provider_name}"
        if assessment.category:
            candidate.category = assessment.category
    return list(candidates)


def apply_narration(candidates: Sequence[Candidate], narrations: Sequence[Any]) -> None:
    """Apply LLM copy where it exists, keeping extractive copy as fallback."""
    for candidate, narration in zip(candidates, narrations):
        if not narration:
            continue
        if getattr(narration, "title", ""):
            candidate.title = narration.title
        if getattr(narration, "hook", ""):
            candidate.hook = narration.hook
        if getattr(narration, "reason", ""):
            candidate.reason = narration.reason
        headlines = getattr(narration, "headline_variants", None)
        if headlines:
            candidate.meta["headline_variants"] = list(headlines)
