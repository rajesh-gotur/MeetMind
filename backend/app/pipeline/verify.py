"""Layer 12 — second-opinion check on the written notes.

1. Coverage (rules): every verified task/decision fact with importance >= 5 must
   appear in the notes; missing ones are added from the fact itself.
2. Reviewer (a DIFFERENT AI than the writer): lists tasks nobody said, wrong
   owners and missing facts. Its suggestions are applied only when the rules agree
   (a task is removed only if its quote is weak; owners must be real attendees).
3. Dates: "next Friday", "end of month", "by 15th" -> real calendar dates (dateparser).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta

from app.pipeline.facts import Fact, facts_block
from app.schemas import ActionItem, Decision, MeetingReport
from app.services.llm import AnalysisError, LanguageModel
from app.services.validator import NameMatcher

logger = logging.getLogger(__name__)

COVERED = 62  # RapidFuzz token-set ratio between a fact and a task/decision that already covers it
MIN_IMPORTANCE = 5

REVIEW_SYSTEM = """You review meeting notes against verified facts from the transcript. Return ONLY JSON.
Report:
  "missing": ids of facts (tasks or decisions) that are important but missing from the notes
  "unsupported_tasks": ids of tasks in the notes that no fact or quote supports
  "owners": [{"task_id": "T-001", "owner": "exact participant name"}] only when the owner is clearly wrong
JSON: {"missing": ["F3"], "unsupported_tasks": ["T-004"], "owners": []}
Use empty lists when the notes are correct."""


def add_missing(report: MeetingReport, facts: list[Fact], names: NameMatcher, meeting_day: date) -> int:
    """Rule-based coverage check. Returns how many items were added."""
    added = 0
    for fact in facts:
        if fact.importance < MIN_IMPORTANCE:
            continue
        if fact.type == "task" and not _task_covered(report, fact):
            report.action_items.append(_task(fact, names, meeting_day, len(report.action_items) + 1))
            added += 1
        elif fact.type == "decision" and not _decision_covered(report, fact):
            report.decisions.append(
                Decision(text=fact.text, made_by=names.match(fact.speaker, default=""), time=fact.time)
            )
            added += 1
    _renumber(report)
    return added


async def review(
    model: LanguageModel, report: MeetingReport, facts: list[Fact], names: NameMatcher, meeting_day: date
) -> tuple[int, int]:
    """Second AI opinion. Returns (items added, items changed or removed)."""
    if not facts:
        return 0, 0
    notes = {
        "tasks": [
            {"id": i.id, "task": i.task, "owner": i.owner, "evidence": i.evidence, "verified": i.verified}
            for i in report.action_items
        ],
        "decisions": [d.text for d in report.decisions],
    }
    prompt = (
        f"PARTICIPANTS: {', '.join(names.participants) or 'not known'}\n\nVERIFIED FACTS:\n{facts_block(facts)}\n\n"
        f"NOTES:\n{json.dumps(notes, ensure_ascii=False)}"
    )
    try:
        raw = await model.complete_json(REVIEW_SYSTEM, prompt, order="reviewer")
    except AnalysisError as exc:
        logger.warning("Reviewer skipped: %s", exc)
        raise
    by_id = {f.id: f for f in facts}
    added = changed = 0
    for fact_id in raw.data.get("missing") or []:
        fact = by_id.get(str(fact_id))
        if fact is None:
            continue
        if fact.type == "task" and not _task_covered(report, fact):
            report.action_items.append(_task(fact, names, meeting_day, len(report.action_items) + 1))
            added += 1
        elif fact.type == "decision" and not _decision_covered(report, fact):
            report.decisions.append(
                Decision(text=fact.text, made_by=names.match(fact.speaker, default=""), time=fact.time)
            )
            added += 1
    unsupported = {str(t) for t in raw.data.get("unsupported_tasks") or []}
    if unsupported:
        before = len(report.action_items)
        # Only tasks whose quote was weak are removed: the reviewer alone never deletes a well-quoted task.
        report.action_items = [i for i in report.action_items if not (i.id in unsupported and not i.verified)]
        changed += before - len(report.action_items)
    for entry in raw.data.get("owners") or []:
        if not isinstance(entry, dict):
            continue
        owner = names.match(str(entry.get("owner") or ""), default="")
        for item in report.action_items:
            if item.id == str(entry.get("task_id")) and owner and item.owner != owner and item.owner == "Unassigned":
                item.owner = owner
                changed += 1
    _renumber(report)
    return added, changed


def resolve_dates(report: MeetingReport, meeting_start: datetime) -> int:
    """Turns spoken deadlines into calendar dates when the writer left them empty."""
    resolved = 0
    for item in report.action_items:
        if item.due_date or not item.due_text:
            continue
        due = deadline(item.due_text, meeting_start)
        if due is not None:
            item.due_date = due
            resolved += 1
    return resolved


_LEAD = re.compile(r"^(?:latest\s+)?(?:by|before|till|until|on|for)\s+|^(?:the\s+)?(?:coming|next|this)\s+(?=\w+day\b)")
_SPECIAL: dict[str, int | str] = {
    "today": 0, "tonight": 0, "eod": 0, "end of day": 0, "end of the day": 0, "aaj": 0,
    "tomorrow": 1, "kal": 1, "day after tomorrow": 2, "parso": 2, "parson": 2,
    "end of week": "week", "end of the week": "week", "end of this week": "week", "this week": "week",
    "end of month": "month", "end of the month": "month", "month end": "month", "this month": "month",
}  # fmt: skip


def deadline(phrase: str, meeting_start: datetime) -> date | None:
    """ "by next Monday", "kal", "end of month", "15th" -> a calendar date on or after the meeting day."""
    day = meeting_start.date()
    text = " ".join(phrase.lower().replace(",", " ").split())
    while (shorter := _LEAD.sub("", text).strip()) != text:  # "by next Monday" -> "monday"
        text = shorter
    special = _SPECIAL.get(text)
    if isinstance(special, int):
        return day + timedelta(days=special)
    if special == "week":
        return day + timedelta(days=(4 - day.weekday()) % 7)  # Friday of this week
    if special == "month":
        first_of_next = (day.replace(day=28) + timedelta(days=4)).replace(day=1)
        return first_of_next - timedelta(days=1)
    try:
        import dateparser
    except ImportError:  # pragma: no cover
        return None
    settings = {
        "RELATIVE_BASE": meeting_start.replace(tzinfo=None),
        "PREFER_DATES_FROM": "future",
        "RETURN_AS_TIMEZONE_AWARE": False,
        # Dates are calendar days in the meeting's own time: never ask the computer for its time zone.
        "TIMEZONE": "UTC",
        "TO_TIMEZONE": "UTC",
    }
    try:
        parsed = dateparser.parse(text, settings=settings)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001 - a date is a nice-to-have, never a reason to fail
        logger.warning("Could not read the deadline %r: %s", phrase, exc.__class__.__name__)
        return None
    if parsed is None:
        return None
    result = parsed.date()
    if result < day and re.fullmatch(r"\d{1,2}(st|nd|rd|th)?", text):  # "15th" said on the 29th = next month
        try:
            result = (day.replace(day=1) + timedelta(days=32)).replace(day=result.day)
        except ValueError:
            return None
    return result if 0 <= (result - day).days <= 400 else None


def _task(fact: Fact, names: NameMatcher, meeting_day: date, number: int) -> ActionItem:
    return ActionItem(
        id=f"T-{number:03d}",
        task=fact.text,
        owner=names.match(fact.owner),
        assigned_by=names.match(fact.assigned_by or fact.speaker, default=""),
        due_text=fact.due_text,
        time=fact.time,
        evidence=fact.quote,
        verified=True,
    )


def _task_covered(report: MeetingReport, fact: Fact) -> bool:
    return any(
        _similar(fact.text, item.task) >= COVERED or _similar(fact.quote, item.evidence) >= COVERED
        for item in report.action_items
    )


def _decision_covered(report: MeetingReport, fact: Fact) -> bool:
    return any(_similar(fact.text, d.text) >= COVERED for d in report.decisions) or any(
        _similar(fact.text, i.task) >= COVERED for i in report.action_items
    )


def _similar(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    try:
        from rapidfuzz import fuzz
    except ImportError:  # pragma: no cover
        from difflib import SequenceMatcher

        return SequenceMatcher(None, a.lower(), b.lower()).ratio() * 100
    return float(fuzz.token_set_ratio(a.lower(), b.lower()))


def _renumber(report: MeetingReport) -> None:
    for number, item in enumerate(report.action_items, start=1):
        item.id = f"T-{number:03d}"
