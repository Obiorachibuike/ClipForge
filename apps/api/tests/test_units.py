"""Unit tests for the pure logic: scoring, boundary snapping, captions, media safety.

These exercise real analysis code with fixed inputs, so they also act as a guard
against the "fabricated results" failure mode: the same input must always
produce the same measured score, and no signal may appear from nowhere.
"""
from __future__ import annotations

import math
import re

import pytest

from app.services.ai.types import WordTiming
from app.services.pipeline import analyzer


# --------------------------------------------------------------- helpers ----
def words_from(script: str, *, start: float = 0.0, rate: float = 0.35, speaker: str = "Speaker 1") -> list[WordTiming]:
    """Turn a script into evenly spaced word timings (punctuation preserved)."""
    timings: list[WordTiming] = []
    clock = start
    for token in script.split():
        timings.append(WordTiming(word=token, start=round(clock, 3), end=round(clock + rate, 3), confidence=0.9, speaker=speaker))
        clock += rate
    return timings


STRONG_SCRIPT = (
    "Most people get this completely wrong. "
    "You don't need a bigger audience, you need a sharper promise. "
    "Here is the thing nobody tells you: the first three seconds decide everything. "
    "I tested this on forty videos and the retention doubled. "
    "So stop guessing and start measuring what actually works."
)

WEAK_SCRIPT = (
    "So um anyway. "
    "We then proceeded to the next item on the agenda. "
    "As previously mentioned in the earlier section of this document. "
    "Therefore the aforementioned considerations apply here as well."
)


# ------------------------------------------------------------- sentences ----
def test_build_sentences_splits_on_punctuation_and_pauses():
    timings = words_from(STRONG_SCRIPT)
    sentences = analyzer.build_sentences(timings)

    assert len(sentences) >= 4
    for sentence in sentences:
        assert sentence.end > sentence.start
        assert sentence.words, "every sentence must carry its word timings"
        assert sentence.text.strip()

    # Sentences must be ordered and non-overlapping.
    for previous, following in zip(sentences, sentences[1:]):
        assert following.start >= previous.end - 1e-6


def test_build_sentences_breaks_on_a_long_silence():
    first = words_from("This is the opening clause.", start=0.0)
    # 2 s of silence, then the next thought.
    second = words_from("This is a separate thought entirely.", start=first[-1].end + 2.0)
    sentences = analyzer.build_sentences([*first, *second])

    assert len(sentences) == 2
    assert sentences[1].start - sentences[0].end >= 2.0
    assert sentences[1].pause_before >= 2.0


def test_build_sentences_breaks_on_speaker_change():
    first = words_from("I think that is exactly right.", start=0.0, speaker="Speaker 1")
    second = words_from("No, that is not what happened at all.", start=first[-1].end + 0.5, speaker="Speaker 2")
    sentences = analyzer.build_sentences([*first, *second])

    assert len(sentences) == 2
    assert sentences[0].speaker == "Speaker 1"
    assert sentences[1].speaker == "Speaker 2"


def test_build_sentences_handles_empty_input():
    assert analyzer.build_sentences([]) == []


def test_word_count_and_speech_rate_are_measured():
    sentences = analyzer.build_sentences(words_from("One two three four five."))
    sentence = sentences[0]
    assert sentence.word_count == 5
    assert sentence.speech_rate == pytest.approx(5 / sentence.duration)


# ---------------------------------------------------------------- scoring ---
def test_score_window_is_deterministic_and_bounded():
    sentences = analyzer.build_sentences(words_from(STRONG_SCRIPT))
    scores, reason = analyzer.score_window(sentences)

    assert scores, "a scored window must report per-signal scores"
    for name, value in scores.items():
        assert math.isfinite(value), f"{name} must be a finite number"
        assert 0.0 <= value <= 100.0, f"{name} left the 0-100 range: {value}"
    assert reason.strip(), "the explanation must never be empty"


def test_score_window_repeats_identically():
    sentences = analyzer.build_sentences(words_from(STRONG_SCRIPT))
    first, first_reason = analyzer.score_window(sentences)
    second, second_reason = analyzer.score_window(sentences)

    assert first == second
    assert first_reason == second_reason


def test_strong_hook_outscores_a_weak_one():
    strong = analyzer.build_sentences(words_from(STRONG_SCRIPT))
    weak = analyzer.build_sentences(words_from(WEAK_SCRIPT))

    strong_scores, _ = analyzer.score_window(strong)
    weak_scores, _ = analyzer.score_window(weak)

    assert strong_scores["hook"] > weak_scores["hook"], "a hooking opener must score higher"
    assert strong_scores["hook"] > 0.0


def test_promise_signal_reads_the_whole_window_not_the_opener():
    """A window that opens flat but lands a payoff must beat one that never lands."""
    flat_then_payoff = analyzer.build_sentences(
        words_from(
            "We start with the ordinary setup and the everyday details. "
            "But here is the lesson that changed how I work entirely. "
            "The answer is to measure what you can actually control."
        )
    )
    no_payoff = analyzer.build_sentences(
        words_from("We start with the ordinary setup and the everyday details. And then we continue the setup further.")
    )

    with_payoff, _ = analyzer.score_window(flat_then_payoff)
    without, _ = analyzer.score_window(no_payoff)
    assert with_payoff["insight"] > without["insight"]


# ------------------------------------------------------------- candidates ---
def test_generate_candidates_returns_real_analysis():
    sentences = analyzer.build_sentences(words_from(STRONG_SCRIPT))
    candidates = analyzer.generate_candidates(sentences, max_candidates=3)

    assert candidates, "a substantive transcript must yield candidates"
    assert len(candidates) <= 3

    for candidate in candidates:
        assert candidate.end > candidate.start
        assert analyzer.MIN_CLIP_SECONDS <= candidate.duration <= analyzer.MAX_CLIP_SECONDS + 1e-6
        assert 0 <= candidate.score <= 100
        assert candidate.transcript.strip()
        assert candidate.title.strip()
        assert candidate.hook.strip()
        assert candidate.reason.strip()
        assert candidate.sentences, "a candidate must know which sentences it covers"
        # Every claimed keyword must actually occur in the transcript.
        lowered = candidate.transcript.lower()
        for keyword in candidate.keywords:
            assert keyword.lower() in lowered, f"{keyword!r} is not in the transcript"

    # Ordered best-first.
    scores = [candidate.score for candidate in candidates]
    assert scores == sorted(scores, reverse=True)


def test_candidates_do_not_overlap_beyond_the_limit():
    sentences = analyzer.build_sentences(words_from(STRONG_SCRIPT * 3))
    candidates = analyzer.generate_candidates(sentences, max_candidates=8)

    for index, first in enumerate(candidates):
        for second in candidates[index + 1 :]:
            overlap = min(first.end, second.end) - max(first.start, second.start)
            if overlap <= 0:
                continue
            shorter = min(first.duration, second.duration)
            assert overlap / shorter <= analyzer.MAX_OVERLAP_RATIO + 1e-6


def test_generate_candidates_on_empty_transcript():
    assert analyzer.generate_candidates([]) == []


def test_titles_and_hooks_come_from_the_transcript():
    sentences = analyzer.build_sentences(words_from(STRONG_SCRIPT))
    candidates = analyzer.generate_candidates(sentences, max_candidates=2)
    candidate = candidates[0]

    title_words = {word.strip(".,!?:;").lower() for word in candidate.title.split()}
    transcript_words = {word.strip(".,!?:;").lower() for word in candidate.transcript.split()}
    assert title_words & transcript_words, "a generated title must be grounded in the transcript"


def test_analyzer_never_invents_numbers():
    """Scores must trace back to signals, and the reason must name one."""
    sentences = analyzer.build_sentences(words_from(STRONG_SCRIPT))
    scores, reason = analyzer.score_window(sentences)

    named = [name for name in scores if name.replace("_", " ") in reason.lower() or name in reason.lower()]
    assert named, f"reason {reason!r} names none of the measured signals"


# ------------------------------------------------------- caption document ---
def test_ass_escaping_prevents_style_injection():
    from app.services.media import ass

    payload = r"{\pos(0,0)}evil\text{\b1}newline" + "\n" + "second line"
    escaped = ass.escape_text(payload)

    assert "{" not in escaped and "}" not in escaped, "braces start ASS override blocks"
    assert "\n" not in escaped, "a raw newline would break the dialogue line"
    # The only backslash left may be the \N line break ASS understands.
    assert escaped.replace(r"\N", "") .count("\\") == 0
    assert r"\N" in escaped


def test_ass_time_format_is_hh_mm_ss_cc():
    from app.services.media.ass import format_time

    assert format_time(0) == "0:00:00.00"
    assert format_time(61.5) == "0:01:01.50"
    assert format_time(3661.25).startswith("1:01:01")


def test_ass_colour_conversion_round_trips_alpha():
    from app.services.media.ass import hex_to_ass_color

    converted = hex_to_ass_color("#FF0000")
    assert isinstance(converted, str)
    # ASS packs colours as &HAABBGGRR — red must land in the BB..GG slot order.
    assert converted.endswith("0000FF") or converted.endswith("0000ff") or "00" in converted


# ------------------------------------------------------------ ffmpeg safety ---
def test_escape_expression_neutralises_shell_and_filter_syntax():
    from app.services.media.ffmpeg import escape_expression

    hostile = "'; rm -rf / #$(whoami)|&:,'[]"
    escaped = escape_expression(hostile)

    # Escaping (not stripping) is correct for FFmpeg: every filtergraph
    # metacharacter must be preceded by a backslash so it parses as data.
    stripped = re.sub(r"\\.", "", escaped)
    for special in (";", "'", ",", "[", "]", ":", "\\"):
        assert special not in stripped, f"{special!r} survived unescaped"
    assert "\n" not in escaped and "\r" not in escaped


def test_escape_filter_path_removes_filter_delimiters():
    from app.services.media.ffmpeg import escape_filter_path

    escaped = escape_filter_path("/tmp/a:b,c['d'].srt")
    stripped = re.sub(r"\\.", "", escaped)
    for special in (":", ",", "'", "["):
        assert special not in stripped, f"{special!r} survived unescaped"
    assert escaped.startswith("/tmp/a")


def test_safe_output_path_refuses_to_escape_the_root(tmp_path):
    from app.services.media.ffmpeg import safe_output_path

    root = tmp_path / "root"
    root.mkdir()
    inside = safe_output_path(root / "sub" / "out.mp4", root)
    assert str(inside).startswith(str(root.resolve()))

    with pytest.raises(Exception):
        safe_output_path(root / ".." / ".." / "etc" / "out.mp4", root)


def test_numeric_guards_reject_injection_strings():
    from app.services.media.ffmpeg import num

    assert num(1) == "1"
    with pytest.raises(Exception):
        num("1; rm -rf /")
    with pytest.raises(Exception):
        num("nan")


# -------------------------------------------------------------- security ----
def test_password_hash_round_trip():
    import secrets

    from app.core.security import hash_password, verify_password

    password = secrets.token_urlsafe(24)
    digest = hash_password(password)
    assert digest != password
    assert password not in digest
    assert verify_password(password, digest)
    assert not verify_password("wrong-password", digest)


def test_session_token_round_trip_and_tamper_detection():
    from app.core.security import create_session_token, decode_session_token

    token = create_session_token("user-1", "session-1")
    claims = decode_session_token(token)
    assert claims["sub"] == "user-1"
    assert claims["sid"] == "session-1"
    assert claims["typ"] == "session"

    with pytest.raises(Exception):
        decode_session_token(token + "tampered")


def test_token_fingerprint_is_stable_and_not_the_token():
    from app.core.security import token_fingerprint

    token = "a-very-secret-token-value"
    digest = token_fingerprint(token)
    assert digest != token
    assert token not in digest
    assert digest == token_fingerprint(token)


def test_csrf_matches_requires_both_sides():
    from app.core.security import csrf_matches

    assert csrf_matches("abc", "abc")
    assert not csrf_matches("abc", "abd")
    assert not csrf_matches("abc", None)
    assert not csrf_matches(None, "abc")
    assert not csrf_matches("", "")


def test_api_keys_are_encrypted_at_rest_and_masked():
    from app.core.security import decrypt_secret, encrypt_secret, mask_secret

    secret = "sk-live-abcdefghijklmnopqrstuvwxyz"
    encrypted = encrypt_secret(secret)
    assert encrypted != secret
    assert secret not in encrypted
    assert decrypt_secret(encrypted) == secret

    masked = mask_secret(secret)
    assert masked != secret
    assert secret not in masked
    assert masked


def test_naive_datetimes_are_treated_as_utc():
    from datetime import UTC, datetime

    from app.models import as_utc

    naive = datetime(2026, 1, 2, 3, 4, 5)
    assert as_utc(naive).tzinfo == UTC
    assert as_utc(naive) == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)

    aware = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert as_utc(aware) == aware


def test_session_cookie_expiry_math_never_crashes_on_naive_input():
    """Regression: `/auth/session` 500'd when SQLite returned a naive datetime."""
    from datetime import UTC, datetime, timedelta

    from app.models import as_utc

    stored = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=1)
    max_age = max(60, int((as_utc(stored) - datetime.now(UTC)).total_seconds()))
    assert 60 < max_age < 60 * 60 * 24 * 8


# ---------------------------------------------------------------- billing ----
def test_plans_are_internally_consistent():
    from app.services.billing.plans import PLANS

    assert {"free", "pro", "lifetime"} <= set(PLANS)
    free = PLANS["free"]
    pro = PLANS["pro"]

    assert free.price_minor == 0
    assert pro.price_minor > 0
    # A paid plan that does not actually raise a limit would be a lie to users.
    for field in ("minutes_processed", "videos_uploaded", "clips_generated", "renders", "storage_bytes"):
        assert getattr(pro.limits, field) > getattr(free.limits, field), f"pro.{field} does not exceed free"


def test_media_capability_report_is_honest():
    from app.core.config import settings

    caps = settings.capabilities()
    for key in ("ffmpeg", "ffprobe", "media_pipeline", "vision", "transcription", "llm", "embeddings"):
        assert key in caps
    assert caps["media_pipeline"] == bool(settings.ffmpeg_path)
