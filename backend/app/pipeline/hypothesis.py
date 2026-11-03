"""What one "ear" (speech engine or caption stream) heard for one piece of audio."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_NORMAL = re.compile(r"[^\w]+", re.UNICODE)


def norm(token: str) -> str:
    """Comparison form of a word: lower case, no punctuation ("ERPNext," -> "erpnext")."""
    return _NORMAL.sub("", token.lower())


@dataclass
class HWord:
    text: str
    start: float  # seconds from the meeting start
    end: float
    confidence: float  # 0-1 as reported (or estimated) for this ear
    speaker: str | None = None  # voice group ("dg:0:1") or a real name (captions)


@dataclass
class Hypothesis:
    source: str  # "gemini", "deepgram", "captions", "groq", "local_whisper", "indic"
    label: str  # human-readable engine name, e.g. "Deepgram nova-3"
    words: list[HWord] = field(default_factory=list)
    timed: bool = True  # False: word times are estimates (engine gave text only)
    language: str | None = None

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    def tokens(self) -> list[str]:
        return [norm(w.text) for w in self.words]


# How much each ear's vote counts when the ears disagree (before word confidence).
SOURCE_WEIGHT = {
    "gemini": 1.0,
    "deepgram": 0.9,
    "indic": 0.85,
    "groq": 0.75,
    "local_whisper": 0.7,
    "captions": 0.65,
}
