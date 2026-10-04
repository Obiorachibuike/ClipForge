"""Ground-truth narration for the ClipForge demo asset.

This is the single source of truth for the demo video's spoken content. The
asset builder synthesises it with espeak-ng (one utterance per sentence, with
real pauses), and the end-to-end test pastes the same text into the project as
the narration script so transcription can be verified against a known truth.
"""
from __future__ import annotations

NARRATION_SECTIONS: list[dict] = [
    {
        "title": "The finishing problem",
        "image": "presenter_1.jpg",
        "pan": "zoom_in",
        "sentences": [
            "Most solo developers do not have a shipping problem. They have a finishing problem.",
            "You start with a plan that feels reasonable: twelve features, three integrations, a landing page, and a launch date.",
            "Six weeks later the code still runs on your machine, and nobody has ever paid you a dollar.",
            "Here is the uncomfortable truth. Nobody cares about your feature list. They care about one problem you solve well.",
            "The fix is not discipline. The fix is deciding what you will not build before you write the first line.",
        ],
    },
    {
        "title": "Ship the smallest useful thing",
        "image": "presenter_2.jpg",
        "pan": "pan_right",
        "sentences": [
            "I want you to write down the one sentence you would say to a customer to explain your product.",
            "If that sentence needs a comma and the word and, it is too big. Delete the second half.",
            "Now build only what that sentence promises, and put it online where real people can reach it.",
            "My first useful version took nine days. It had one button, one form, and no settings page at all.",
            "It made four hundred dollars that month, which is not a business, but it was proof that strangers would pay.",
            "That proof changed every decision I made after it, and no amount of planning would have given it to me.",
        ],
    },
    {
        "title": "Feedback loops beat features",
        "image": "presenter_3.jpg",
        "pan": "pan_left",
        "sentences": [
            "The most valuable asset in a young product is not code. It is the speed of your feedback loop.",
            "Every week you want one real conversation with someone who used the thing you shipped.",
            "Ask what they tried to do, where they hesitated, and what they did instead when it failed.",
            "You will hear the same complaint three times before you are allowed to fix it, because the first two times it is noise.",
            "When the third person says it, you stop being clever and you make the change that removes the whole problem.",
        ],
    },
    {
        "title": "Build the boring engine",
        "image": "presenter_4.jpg",
        "pan": "zoom_out",
        "sentences": [
            "Around month three, growth stops being about the product and starts being about the engine around it.",
            "That engine is boring: a landing page that converts, a churn email, an onboarding step, and a payment flow that never breaks.",
            "Automate the parts you do more than twice a week. Ignore the parts you do once a quarter.",
            "Track four numbers: signups, activation rate, weekly retention, and revenue. Everything else is a distraction.",
            "When activation goes up, you scale acquisition. When retention drops, you stop selling and fix the product.",
            "That is the whole game. Ship the smallest useful thing, talk to the people who use it, and let the boring engine compound.",
        ],
    },
]

# Long pause between sections keeps clip discovery's pause detection meaningful.
SENTENCE_PAUSE = 0.32
SECTION_PAUSE = 1.10
RATE = 166
VOICE = "en"


def narration_text() -> str:
    """The full script as one string (what a creator would paste in)."""
    return " ".join(sentence for section in NARRATION_SECTIONS for sentence in section["sentences"])


def section_titles() -> list[str]:
    return [section["title"] for section in NARRATION_SECTIONS]
