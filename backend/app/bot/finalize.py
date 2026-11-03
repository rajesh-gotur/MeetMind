"""After the meeting: layers 5-12 for the bot, from what was saved during the call.

Used right after the call and again by "Create notes again" (from the saved
meeting folder; the speech engines are not called a second time).

  L5 + L7  each piece's ears + the (now complete) captions -> word vote
  L8       glossary fixer        L9  AI fixer on doubtful words
  L10      speaker vote          L11-L12 notes (services/notes.py)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.config import Settings
from app.pipeline import learned_terms
from app.pipeline.ai_fix import AiFixer
from app.pipeline.glossary import GlossaryFixer
from app.pipeline.scores import QualityInputs
from app.pipeline.speakers import attribute
from app.pipeline.transcriber import Hearing, Transcriber
from app.schemas import AnalyzeRequest, AnalyzeResponse, MeetingInfo
from app.services.llm import LanguageModel
from app.services.notes import NotesExtras, NotesWriter

logger = logging.getLogger("meetmind.bot")


class NothingRecordedError(RuntimeError):
    """Neither the audio nor the captions contain any speech."""


@dataclass
class MeetingData:
    """Everything the bot collected; saved as meeting.json in the meeting folder."""

    code: str
    title: str
    bot_name: str
    requested_by: str
    started_at: float  # epoch seconds when recording started
    ended_at: float
    started_wall: str  # ISO time with the local UTC offset
    roster: list[str] = field(default_factory=list)
    captions: list[dict[str, Any]] = field(default_factory=list)
    timeline: list[dict[str, Any]] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)  # watchdog findings (sound missing, repairs)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MeetingData:
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})


@dataclass(frozen=True)
class BotResult:
    meeting: MeetingInfo
    notes: AnalyzeResponse


async def build_notes(
    settings: Settings, data: MeetingData, hearings: list[Hearing], terms: list[str], step: Any = None
) -> BotResult:
    """Runs layers 5-12. ``step(text)`` reports progress to the popup."""
    report = step or (lambda _text: None)
    transcriber = Transcriber(settings)
    try:
        report("Checking the words: comparing every ear with the captions…")
        fixer = GlossaryFixer(terms, settings.pipeline.doubt_below)
        words = transcriber.assemble(hearings, data.captions, terms, data.bot_name, fixer)
    finally:
        await transcriber.aclose()
    if not words:
        raise NothingRecordedError("No speech was recorded (no sound and no captions), so there are no notes.")

    fallbacks = list(data.gaps)
    corrected = sum(1 for w in words if w.corrected)
    if settings.pipeline.ai_fixer:
        report("Fixing doubtful words…")
        ai = AiFixer(LanguageModel(settings), terms, data.roster, settings.pipeline.max_doubts_per_call)
        words, changed = await ai.fix_words(words, settings.pipeline.doubt_below)
        corrected += changed
        if ai.failed:
            fallbacks.append("AI word fixer: no AI answered; the best-voted words were kept.")
    if settings.vocabulary.learn:
        learned_terms().remember(fixer.fixes)

    report("Naming the speakers…")
    attributed = attribute(words, data.captions, data.timeline, data.roster, data.bot_name)
    duration = max(0.0, data.ended_at - data.started_at)
    quality = QualityInputs(
        meeting_seconds=duration,
        covered_seconds=_covered(hearings, data.captions, duration),
        words=words,
        speaker_confirmed=attributed.confirmed,
        ears=[*(e for h in hearings for e in h.ears), *(["Meet captions"] if data.captions else [])],
        fallbacks=[*fallbacks, *_condense(h.failures for h in hearings)],
        corrected=corrected,
    )
    if not data.captions:
        quality.fallbacks.append("Captions were not available in the bot's window; names come from voices only.")

    meeting = MeetingInfo(
        title=(data.title or f"Meeting {data.code}")[:200],
        code=data.code[:60],
        started_at=datetime.fromisoformat(data.started_wall),
        duration_seconds=round(duration),
        # The bot is the recorder, so no voice is "the recorder" and every voice can be named.
        recorded_by=data.bot_name,
    )
    report("Writing the notes: facts, summary and tasks…")
    notes = await NotesWriter(settings).write(
        AnalyzeRequest(
            meeting=meeting,
            participants=attributed.participants,
            turns=attributed.turns,
            unresolved_speakers=attributed.unresolved,
        ),
        NotesExtras(quality=quality, glossary=terms),
    )
    return BotResult(meeting=meeting, notes=notes)


def _covered(hearings: list[Hearing], captions: list[dict[str, Any]], duration: float) -> float:
    """Seconds of the meeting with usable sound, or with captions where the sound was missing."""
    covered = 0.0
    for hearing in hearings:
        heard = hearing.skipped or any(h.words for h in hearing.hypotheses)
        captioned = any(hearing.offset <= float(c["start"]) < hearing.end for c in captions)
        if heard or captioned:
            covered += hearing.duration
    return min(covered, duration) if duration else covered


def _condense(failure_lists: Any) -> list[str]:
    engines = list(dict.fromkeys(f.split(" could not")[0] for failures in failure_lists for f in failures))
    return [f"{name} failed on some pieces; the other ears covered them." for name in engines]
