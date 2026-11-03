"""The notes pipeline shared by Record here (/analyze) and the bot.

  L9   AI fixer on doubtful words (Record here: doubts saved while the pieces were transcribed)
  L11  facts first: facts per section, each quote verified -> the writer AI writes the notes from them
       (the same call also proposes spelling fixes and speaker names, checked by refine.safe_fix)
  L12  coverage rules + reviewer AI (a different model) + calendar dates + validator
  Quality scores for the popup and the files

If an optional step fails (no AI answered the facts or the review), the notes are
still written and the Quality section says which step was skipped.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.config import Settings
from app.pipeline.ai_fix import AiFixer, Doubt
from app.pipeline.facts import Fact, extract_facts, facts_block
from app.pipeline.scores import QualityInputs, score
from app.pipeline.verify import add_missing, resolve_dates, review
from app.schemas import AnalyzeRequest, AnalyzeResponse
from app.services.llm import AnalysisError, MeetingAnalyzer
from app.services.refine import TranscriptRefiner
from app.services.validator import NameMatcher, is_placeholder, validate_report

__all__ = ["AnalysisError", "EmptyTranscriptError", "NotesExtras", "NotesWriter"]

logger = logging.getLogger(__name__)


class EmptyTranscriptError(ValueError):
    """Nothing was said, so there is nothing to summarise."""


@dataclass
class NotesExtras:
    """What the transcript layers learned, passed on to the notes and the quality score."""

    doubts: list[Doubt] = field(default_factory=list)
    quality: QualityInputs | None = None
    glossary: list[str] = field(default_factory=list)


class NotesWriter:
    def __init__(self, settings: Settings, analyzer: MeetingAnalyzer | None = None) -> None:
        self._settings = settings
        self._analyzer = analyzer or MeetingAnalyzer(settings)

    async def write(self, request: AnalyzeRequest, extras: NotesExtras | None = None) -> AnalyzeResponse:
        """Raises EmptyTranscriptError, or AnalysisError when no model could write the notes."""
        if not any(turn.text.strip() for turn in request.turns):
            raise EmptyTranscriptError("The transcript is empty, so there is nothing to summarise.")
        extras = extras or NotesExtras()
        settings = self._settings
        model = self._analyzer.model
        glossary = extras.glossary or settings.vocabulary.terms
        quality = extras.quality or QualityInputs(meeting_seconds=request.meeting.duration_seconds, covered_seconds=0)
        fallbacks = list(quality.fallbacks)

        # ---- L9 (Record here): doubtful words saved during the meeting
        turns = request.turns
        if extras.doubts and settings.pipeline.ai_fixer:
            fixer = AiFixer(model, glossary, request.participants, settings.pipeline.max_doubts_per_call)
            turns, changed = await fixer.fix_turns(turns, extras.doubts)
            quality.corrected += changed
            if fixer.failed:
                fallbacks.append("AI word fixer: no AI answered; the best-voted words were kept.")
        request = request.model_copy(update={"turns": turns})

        refiner = TranscriptRefiner(glossary)
        prepared = refiner.prepare(
            request.turns, request.participants, request.meeting.recorded_by, request.unresolved_speakers
        )
        named = request.model_copy(update={"turns": prepared.turns, "participants": prepared.participants})

        # ---- L11a facts first
        facts: list[Fact] = []
        try:
            facts = await extract_facts(model, named, glossary, settings.notes.section_minutes)
        except Exception as exc:  # noqa: BLE001 - the notes are still written from the transcript
            logger.warning("Facts step skipped: %s", exc)
        if not facts:
            fallbacks.append("Facts step: no verified facts; the notes were written from the transcript only.")

        # ---- L11b writer (+ spelling fixes and speaker names)
        raw = await self._analyzer.analyze(named, refiner.open_placeholders(prepared), facts_block(facts), glossary)
        refiner.apply(raw.data, prepared)
        refined = refiner.finish(prepared)
        corrected = request.model_copy(update={"turns": refined.turns, "participants": refined.participants})
        report = validate_report(raw.data, corrected, raw.model)
        report.corrections = refined.corrections + quality.corrected
        report.notes = [*refined.notes, *report.notes]

        # ---- L12 second opinion
        people = [p for p in corrected.participants if not is_placeholder(p)]
        names = NameMatcher(people, corrected.meeting.recorded_by)
        day = corrected.meeting.started_at.date()
        added = add_missing(report, facts, names, day)
        if settings.notes.review and facts:
            try:
                more, changed = await review(model, report, facts, names, day)
                added += more
                if changed:
                    report.notes.append(f"The reviewer AI corrected or removed {changed} task(s).")
            except AnalysisError:
                fallbacks.append("Reviewer AI: no second model answered; the rule checks still ran.")
        if added:
            report.notes.append(f"{added} task(s)/decision(s) were added because the notes had missed them.")
        dates = resolve_dates(report, corrected.meeting.started_at)
        if dates:
            report.notes.append(f"{dates} spoken deadline(s) were turned into calendar dates.")

        quality.fallbacks = fallbacks
        quality.corrected = report.corrections
        report.quality = score(quality, report, refined.turns, corrected.meeting.recorded_by)
        return AnalyzeResponse(report=report, turns=refined.turns, participants=refined.participants)
