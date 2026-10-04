"""Lexicons used by the discovery engine.

These are ordinary linguistic feature lists, not AI magic: each marker maps to a
specific measurable signal (hook, surprise, emotion, insight, storytelling) and
contributes a documented amount to a candidate's score.
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------- structure ---
SENTENCE_END_RE = re.compile(r"[.!?…]+[\"')\]]*$")
FILLER_WORDS = frozenset(
    """um uh erm ah hmm mm like y'know basically literally actually just sort of kind of
    i mean you know right okay so yeah well anyway""".split()
)

# A clip that opens with these depends on missing context.
CONTEXT_DEPENDENT_OPENERS = frozenset(
    """and but so because also then that this it they he she we you those these there here
    however therefore plus anyway ok okay yeah right well now""".split()
)

BACK_REFERENCE_PHRASES = (
    "as i said",
    "like i mentioned",
    "earlier",
    "remember when",
    "we talked about",
    "as we discussed",
    "the previous",
    "before we",
    "coming back to",
)

# -------------------------------------------------------------------- hooks ---
HOOK_PHRASES = (
    "the biggest mistake",
    "the number one",
    "most people",
    "nobody tells you",
    "no one talks about",
    "here's the thing",
    "here is the thing",
    "the truth is",
    "the problem is",
    "what if",
    "let me tell you",
    "i used to",
    "stop doing",
    "you should never",
    "never do this",
    "the reason most",
    "this is why",
    "that's why",
    "the secret",
    "what most",
    "everyone thinks",
    "the key is",
    "if you want",
    "pay attention",
    "listen",
    "watch this",
    "check this out",
    "i learned",
    "i realized",
    "here's how",
    "here is how",
)
HOOK_LEAD_WORDS = frozenset(
    """why how what when where who never always stop start imagine listen look watch here
    most everyone nobody nothing this that these those the there""".split()
)

# ----------------------------------------------------------------- surprise ---
SURPRISE_PHRASES = (
    "turns out",
    "it turned out",
    "surprisingly",
    "unexpectedly",
    "believe it or not",
    "contrary to",
    "shockingly",
    "i was wrong",
    "we were wrong",
    "almost nobody",
    "almost everyone",
    "the opposite",
    "actually works",
    "actually fails",
    "not what you think",
    "counterintuitive",
    "surprised me",
    "blew my mind",
    "changed my mind",
)
CONTRAST_MARKERS = frozenset("but however although though whereas yet instead actually surprisingly meanwhile".split())

# ------------------------------------------------------------------ emotion ---
EMOTION_WORDS = {
    "high": frozenset(
        """insane crazy wild unbelievable incredible amazing awesome terrible awful horrible
        devastating heartbreaking hilarious funny terrifying scary shocking brilliant genius
        love hate obsessed furious angry ecstatic thrilled desperate painful brutal""".split()
    ),
    "medium": frozenset(
        """happy sad excited nervous proud embarrassed frustrated grateful passionate inspired
        disappointed worried thrilled lucky grateful""".split()
    ),
}

# ------------------------------------------------------------------ insight ---
INSIGHT_PHRASES = (
    "the reason",
    "because",
    "so that",
    "which means",
    "the key",
    "what you should",
    "what you need",
    "the lesson",
    "i learned that",
    "the takeaway",
    "in other words",
    "for example",
    "the difference",
    "the point is",
    "framework",
    "principle",
    "strategy",
    "approach",
    "process",
    "step one",
    "first step",
    "rule of thumb",
    "best practice",
    "mistake",
    "tip",
)
LESSON_MARKERS = frozenset(
    """should must need avoid always never better worse improve fix solve build learn teach
    understand realize realize remember recommend suggest""".split()
)

# -------------------------------------------------------------------- story ---
STORY_PHRASES = (
    "when i was",
    "one day",
    "so i",
    "and then",
    "the first time",
    "ended up",
    "at that point",
    "a few years ago",
    "last year",
    "back in",
    "i remember",
    "we started",
    "he said",
    "she said",
    "i said",
    "long story short",
    "to make a long story short",
    "eventually",
    "in the end",
)
PAST_TENSE_HINTS = ("ed ", "was ", "were ", "had ", "did ")

# ------------------------------------------------------------- controversy ---
CONTROVERSY_PHRASES = (
    "controversial",
    "unpopular opinion",
    "hot take",
    "people will hate",
    "i disagree",
    "wrong about",
    "the myth",
    "lies",
    "overrated",
    "underrated",
    "scam",
    "nonsense",
    "bad advice",
)

# ------------------------------------------------------------------- humor ----
HUMOR_PHRASES = ("haha", "lol", "joke", "kidding", "hilarious", "laughed", "comedy", "punchline", "funny thing")
HUMOR_WORDS = frozenset("haha lol kidding hilarious funny comedy laughed ridiculous absurd".split())

# ------------------------------------------------------------------ numbers ---
NUMBER_RE = re.compile(r"\b\d+(?:[.,]\d+)?\s?(?:%|percent|k|m|b|million|billion|thousand|years?|months?|days?|x)?\b")
STATISTIC_RE = re.compile(r"\b\d+(?:\.\d+)?\s?(?:%|percent)\b", re.IGNORECASE)

# ----------------------------------------------------------------- category ---
CATEGORY_MARKERS: dict[str, tuple[str, ...]] = {
    "question": ("?", "why ", "how ", "what if", "have you ever", "do you"),
    "story": STORY_PHRASES,
    "lesson": ("lesson", "learned", "advice", "tip", "should", "never", "always", "mistake"),
    "funny": HUMOR_PHRASES,
    "emotional": ("love", "hate", "cried", "scared", "proud", "hurt", "lost", "changed my life"),
    "controversial": CONTROVERSY_PHRASES,
    "explanation": ("the reason", "because", "which means", "how it works", "in other words", "for example"),
    "insight": ("the key", "the point", "takeaway", "framework", "principle", "truth"),
}


def contains_any(text: str, phrases: tuple[str, ...] | frozenset[str] | list[str]) -> int:
    lowered = f" {text.lower()} "
    return sum(1 for phrase in phrases if phrase in lowered)


def word_set(text: str) -> set[str]:
    return set(re.findall(r"[a-z']+", text.lower()))
