"""Ear 3 — Meet captions as a hypothesis (bot mode).

The bot reads Meet's live captions in its own window (nobody else sees them).
Captions are Google's own speech recognition with exact speaker names, but they
are shown a little late and are sometimes shortened, so they are a second opinion
and a backup, not the final text.
"""

from __future__ import annotations

from typing import Any

from app.pipeline.hypothesis import HWord, Hypothesis

CAPTION_LAG_SECONDS = 0.8  # captions appear about this long after the words are spoken
CAPTION_CONFIDENCE = 0.85


def caption_hypothesis(captions: list[dict[str, Any]], start: float, end: float, bot_name: str) -> Hypothesis:
    """Caption words spoken between start and end (meeting seconds), spread over each caption's time."""
    bot = bot_name.strip().lower()
    words: list[HWord] = []
    for caption in sorted(captions, key=lambda c: c["start"]):
        speaker = str(caption.get("speaker") or "").strip()
        if not speaker or speaker.lower() == bot:
            continue
        c_start = max(0.0, float(caption["start"]) - CAPTION_LAG_SECONDS)
        c_end = max(c_start + 0.3, float(caption.get("end") or caption["start"]) - CAPTION_LAG_SECONDS * 0.5)
        tokens = str(caption.get("text") or "").split()
        if not tokens:
            continue
        step = (c_end - c_start) / len(tokens)
        for i, token in enumerate(tokens):
            t0 = c_start + i * step
            if start <= t0 + step / 2 < end:
                words.append(HWord(token, round(t0, 3), round(t0 + step, 3), CAPTION_CONFIDENCE, speaker))
    return Hypothesis("captions", "Meet captions", words, timed=True)
