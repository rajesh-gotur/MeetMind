"""Quality scores shown in the popup and in the "Quality" section of MOM.md and the Word report.

All scores are 0-100 and honestly labelled "estimated": they measure agreement
between independent signals, not a comparison with a human transcript.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.pipeline.fusion import FusedWord
from app.schemas import MeetingReport, QualityScore, Turn
from app.services.validator import is_placeholder

CONFIDENT = 0.85
WEIGHTS = {"capture": 0.25, "words": 0.35, "speakers": 0.2, "notes": 0.2}


@dataclass
class QualityInputs:
    meeting_seconds: float
    covered_seconds: float  # seconds with usable sound or captions
    words: list[FusedWord] = field(default_factory=list)
    speaker_confirmed: float | None = None  # 0-1 (bot); None: estimate from the turns
    ears: list[str] = field(default_factory=list)
    fallbacks: list[str] = field(default_factory=list)
    corrected: int = 0
    word_counts: tuple[int, int] | None = None  # (confident, total) when the words are not kept (Record here)
    doubtful: int | None = None


def score(inputs: QualityInputs, report: MeetingReport, turns: list[Turn], recorded_by: str) -> QualityScore:
    capture = _pct(inputs.covered_seconds / inputs.meeting_seconds) if inputs.meeting_seconds > 0 else 0
    if inputs.words:
        sure = sum(1 for w in inputs.words if w.agreement >= 2 or w.confidence >= CONFIDENT)
        words = _pct(sure / len(inputs.words))
    elif inputs.word_counts and inputs.word_counts[1]:
        words = _pct(inputs.word_counts[0] / inputs.word_counts[1])
    else:
        words = 0
    speakers = (
        _pct(inputs.speaker_confirmed) if inputs.speaker_confirmed is not None else _turn_speakers(turns, recorded_by)
    )
    items = report.action_items
    notes = _pct(sum(1 for i in items if i.verified) / len(items)) if items else 100
    overall = round(
        WEIGHTS["capture"] * capture
        + WEIGHTS["words"] * words
        + WEIGHTS["speakers"] * speakers
        + WEIGHTS["notes"] * notes
    )
    return QualityScore(
        overall=max(0, min(overall, 100)),
        capture=capture,
        words=words,
        speakers=speakers,
        notes=notes,
        ears=list(dict.fromkeys(inputs.ears)),
        fallbacks=list(dict.fromkeys(inputs.fallbacks)),
        doubtful_words=inputs.doubtful
        if inputs.doubtful is not None
        else sum(1 for w in inputs.words if w.confidence < 0.6),
        corrected_words=inputs.corrected,
    )


def _turn_speakers(turns: list[Turn], recorded_by: str) -> int:
    """Record here: the recorder's lines come from their own microphone; others are sure when named."""
    total = sum(len(t.text.split()) for t in turns)
    if not total:
        return 0
    sure = sum(
        len(t.text.split())
        for t in turns
        if t.speaker.strip().lower() == recorded_by.strip().lower() or not is_placeholder(t.speaker)
    )
    return _pct(sure / total)


def _pct(value: float) -> int:
    return max(0, min(100, round(value * 100)))
