"""Clip-quality regressions found by running the real pipeline.

The first real end-to-end run produced a candidate that opened on the lone word
"noise." (the tail of a sentence split by the duration cap) and a title that
ended "...clever and you". Both are visible editing defects, not scoring
preferences, so they get explicit regression tests.
"""
from __future__ import annotations

from app.services.ai.types import WordTiming
from app.services.pipeline.analyzer import (
    Candidate,
    Sentence,
    _trim_trailing_clause,
    build_sentences,
    extractive_title,
    generate_candidates,
)


def _sentence(text: str, start: float, end: float, *, words: int | None = None) -> Sentence:
    parts = text.split()
    count = words if words is not None else len(parts)
    step = (end - start) / max(1, count)
    timings = [
        WordTiming(word=part, start=start + i * step, end=start + (i + 1) * step)
        for i, part in enumerate(parts[:count])
    ]
    return Sentence(text=text, start=start, end=end, words=timings)


def _candidate(first: Sentence) -> Candidate:
    return Candidate(
        start=first.start,
        end=first.end,
        sentences=[first],
        transcript=first.text,
        score=50,
        scores={},
        reason="",
        title="",
        hook="",
        category="insight",
        keywords=[],
        speaker="",
        analyzer="structural-v1",
    )


def test_a_window_never_starts_on_a_two_word_fragment():
    sentences = [
        _sentence("You will hear the same complaint three times before you are allowed to fix it,", 0.0, 8.0),
        _sentence("because the first two times it is noise.", 8.2, 9.9),
        _sentence("noise.", 9.6, 9.9, words=1),
        _sentence("When the third person says it, you stop being clever.", 10.2, 14.0),
        _sentence("You make the change that removes the whole problem.", 14.2, 18.0),
        _sentence("Every week you want one real conversation.", 18.3, 22.0),
    ]
    candidates = generate_candidates(sentences, max_candidates=6, min_duration=8.0, max_duration=30.0)
    assert candidates, "the synthetic transcript must still yield candidates"
    for candidate in candidates:
        assert candidate.sentences[0].word_count > 2 or candidate.start == sentences[0].start


def test_a_fragment_start_extends_back_to_include_its_context():
    sentences = [
        _sentence("The first half of the sentence explains the idea and sets up the point", 0.0, 6.0),
        _sentence("it.", 6.1, 6.3, words=1),
        _sentence("Then the second half lands the conclusion you came for.", 6.5, 10.0),
        _sentence("Another complete thought follows it with enough words to matter.", 10.2, 14.0),
    ]
    candidates = generate_candidates(sentences, max_candidates=10, min_duration=6.0, max_duration=30.0)
    starts = {round(c.start, 2) for c in candidates}
    assert 6.1 not in starts, "a window must not begin on the orphaned 'it.'"
    assert 0.0 in starts, "the context sentence should be usable as a window start"


def test_title_does_not_end_on_a_dangling_conjunction():
    sentence = _sentence(
        "When the third person says it, you stop being clever and you make the change", 0.0, 5.0
    )
    title = extractive_title(_candidate(sentence))
    assert title == "When the third person says it"
    assert not title.lower().endswith((" and", " you", " the", " to"))


def test_trailing_clause_trimmer_keeps_titles_without_punctuation():
    assert _trim_trailing_clause("Most solo developers do not have a shipping problem") == (
        "Most solo developers do not have a shipping problem"
    )
    assert _trim_trailing_clause("The most valuable asset in a young product is not code") == (
        "The most valuable asset in a young product is not code"
    )
    assert _trim_trailing_clause("short") == "short"


def test_sentence_tails_are_detected_by_word_count():
    from app.services.pipeline.analyzer import _is_fragment

    assert _is_fragment(_sentence("it.", 0.0, 0.2, words=1))
    assert _is_fragment(_sentence("and then", 0.0, 0.4, words=2))
    assert not _is_fragment(_sentence("A real sentence with content.", 0.0, 2.0))


def test_build_sentences_still_groups_real_speech():
    words = [
        WordTiming(word="Most", start=0.0, end=0.2),
        WordTiming(word="solo", start=0.2, end=0.4),
        WordTiming(word="developers", start=0.4, end=0.8),
        WordTiming(word="ship.", start=0.8, end=1.2),
        WordTiming(word="They", start=3.0, end=3.2),
        WordTiming(word="finish.", start=3.2, end=3.6),
    ]
    sentences = build_sentences(words)
    assert [s.text for s in sentences] == ["Most solo developers ship.", "They finish."]


def test_crop_expression_only_uses_keyframes_inside_the_clip():
    """A 40 s clip of a long video must not embed the whole source timeline.

    The old filter started at `clip_start` and ran to the end of the video, so
    the crop expression grew with the source: thousands of nested `if()`s for a
    long recording, most unreachable.
    """
    from app.services.media.framing import crop_expression

    keyframes = [
        {"time": t, "width": 608, "height": 1080, "center_x": 600 + (t * 7) % 800, "center_y": 540}
        for t in range(0, 1200, 2)
    ]
    width, height, x_expr, _y_expr = crop_expression(
        keyframes, clip_start=300, clip_end=340, source_width=1920, source_height=1080
    )
    assert (width, height) == (608, 1080)
    assert x_expr.count("if(") <= 21  # 20 seconds of samples, not 600
    assert "(t-40.000)" in x_expr or "lt(t,40.000)" in x_expr
    assert len(x_expr) < 2000
    assert "t-300" not in x_expr and "t-600" not in x_expr


def test_crop_expression_holds_the_last_known_frame_when_the_window_has_none():
    from app.services.media.framing import crop_expression

    keyframes = [
        {"time": 2.0, "width": 608, "height": 1080, "center_x": 700, "center_y": 540},
        {"time": 4.0, "width": 608, "height": 1080, "center_x": 720, "center_y": 540},
    ]
    width, height, x_expr, _ = crop_expression(
        keyframes, clip_start=20.0, clip_end=30.0, source_width=1920, source_height=1080
    )
    assert (width, height) == (608, 1080)
    assert x_expr == "416.0"  # 720 - 608/2, held for the whole clip
