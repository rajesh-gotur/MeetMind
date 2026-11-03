"""Turns raw model output into a trustworthy MeetingReport.

Every name is matched to the real participants, every task must be backed
by words actually spoken in the transcript, dates must be real and not in
the past, and all times are clamped to the meeting length.
"""

from __future__ import annotations

import re
from datetime import date
from difflib import SequenceMatcher, get_close_matches
from typing import Any

from app.schemas import ActionItem, AnalyzeRequest, Blocker, Decision, MeetingReport, Topic, Turn

UNASSIGNED = "Unassigned"
VERIFIED_SCORE = 0.6  # share of quote words found in one transcript line
REJECT_SCORE = 0.35  # below this the task is treated as invented and dropped
PRIORITIES = {"high": "High", "medium": "Medium", "low": "Low"}

_WORD = re.compile(r"[a-z0-9ऀ-ॿ']+")
# Labels used when the real name of a voice is not known.
PLACEHOLDER = re.compile(r"^(Speaker \d+|Participant|Unknown speaker|Unclear)$", re.IGNORECASE)


def is_placeholder(name: str) -> bool:
    return bool(PLACEHOLDER.match(name.strip()))


def words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


class NameMatcher:
    """Maps a name the model wrote to one of the real participants."""

    def __init__(self, participants: list[str], recorded_by: str) -> None:
        self.participants = [p for p in dict.fromkeys(participants) if p.strip()]
        self.recorded_by = recorded_by.strip()
        self._lower = {p.lower(): p for p in self.participants}

    def match(self, name: str | None, default: str = UNASSIGNED) -> str:
        raw = (name or "").strip()
        if not raw or raw.lower() in {"unassigned", "unknown", "none", "n/a", "everyone", "team", "all"}:
            return default
        if raw.lower() in {"you", "me", "i"} and self.recorded_by:
            return self.recorded_by
        if raw.lower() in self._lower:
            return self._lower[raw.lower()]
        # First-name or surname match, only when it points to exactly one person.
        parts = set(words(raw))
        hits = [p for p in self.participants if parts & set(words(p))]
        if len(hits) == 1:
            return hits[0]
        close = get_close_matches(raw.lower(), list(self._lower), n=1, cutoff=0.82)
        return self._lower[close[0]] if close else default


class EvidenceFinder:
    """Finds the transcript line that best supports a quote."""

    def __init__(self, turns: list[Turn]) -> None:
        self.turns = turns
        self._turn_words = [set(words(t.text)) for t in turns]

    def best(self, quote: str) -> tuple[float, Turn | None]:
        quote_words = words(quote)
        if not quote_words or not self.turns:
            return 0.0, None
        best_score, best_turn = 0.0, None
        for turn, vocab in zip(self.turns, self._turn_words, strict=True):
            score = sum(1 for w in quote_words if w in vocab) / len(quote_words)
            if score > best_score:
                best_score, best_turn = score, turn
        return best_score, best_turn


def _text(value: Any, limit: int = 2000) -> str:
    return str(value or "").strip()[:limit]


def _seconds(value: Any, duration: float) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(min(max(number, 0.0), max(duration, 0.0)), 1)


def _due_date(value: Any, meeting_day: date) -> date | None:
    try:
        parsed = date.fromisoformat(_text(value, 10))
    except ValueError:
        return None
    # A deadline before the meeting, or absurdly far away, is a model mistake.
    if parsed < meeting_day or (parsed - meeting_day).days > 400:
        return None
    return parsed


def _similar(a: str, b: str) -> bool:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio() > 0.85


def validate_report(raw: dict[str, Any], request: AnalyzeRequest, model: str) -> MeetingReport:
    meeting = request.meeting
    duration = meeting.duration_seconds or (request.turns[-1].end if request.turns else 0)
    participants = list(dict.fromkeys([*request.participants, *(t.speaker for t in request.turns)]))
    names = NameMatcher([p for p in participants if not is_placeholder(p)], meeting.recorded_by)
    evidence = EvidenceFinder(request.turns)
    notes: list[str] = []

    # ---- action items: the part people act on, so it gets the strictest checks
    items: list[ActionItem] = []
    dropped = 0
    for entry in raw.get("action_items") or []:
        if not isinstance(entry, dict):
            continue
        task = _text(entry.get("task"), 500)
        if not task:
            continue
        quote = _text(entry.get("evidence"), 400)
        score, turn = evidence.best(quote or task)
        if score < REJECT_SCORE:
            dropped += 1
            continue
        time = _seconds(entry.get("time"), duration)
        # The line that contains the quote is the source of truth for when it was said.
        if turn is not None and (time is None or not turn.start - 10 <= time <= turn.end + 10):
            time = round(turn.start, 1)
        assigned_by = names.match(entry.get("assigned_by"), default="")
        if not assigned_by and turn is not None:
            assigned_by = names.match(turn.speaker, default="")
        if any(_similar(task, existing.task) for existing in items):
            continue
        items.append(
            ActionItem(
                id=f"T-{len(items) + 1:03d}",
                task=task,
                owner=names.match(entry.get("owner")),
                assigned_by=assigned_by,
                due_date=_due_date(entry.get("due_date"), meeting.started_at.date()),
                due_text=_text(entry.get("due_text"), 120),
                priority=PRIORITIES.get(_text(entry.get("priority")).lower(), "Medium"),  # type: ignore[arg-type]
                time=time or 0.0,
                evidence=quote or (turn.text[:200] if turn else ""),
                verified=score >= VERIFIED_SCORE,
            )
        )
    if dropped:
        notes.append(f"{dropped} suggested task(s) were removed because nobody said them in the meeting.")
    unverified = sum(1 for item in items if not item.verified)
    if unverified:
        notes.append(f"{unverified} task(s) are marked 'Please check' because the wording was only partly found.")

    # ---- topics, in time order and inside the meeting length
    topics: list[Topic] = []
    for entry in raw.get("topics") or []:
        if not isinstance(entry, dict) or not _text(entry.get("title")):
            continue
        start = _seconds(entry.get("start"), duration) or 0.0
        end = _seconds(entry.get("end"), duration)
        topics.append(
            Topic(
                title=_text(entry.get("title"), 200),
                summary=_text(entry.get("summary")),
                start=start,
                end=end if end is not None and end >= start else start,
                speakers=[n for n in (names.match(s, default="") for s in entry.get("speakers") or []) if n],
            )
        )
    topics.sort(key=lambda t: t.start)

    decisions = [
        Decision(
            text=_text(d.get("text"), 500),
            made_by=names.match(d.get("made_by"), default=""),
            time=_seconds(d.get("time"), duration),
        )
        for d in raw.get("decisions") or []
        if isinstance(d, dict) and _text(d.get("text"))
    ]
    blockers = [
        Blocker(
            text=_text(b.get("text"), 500),
            raised_by=names.match(b.get("raised_by"), default=""),
            time=_seconds(b.get("time"), duration),
        )
        for b in raw.get("blockers") or []
        if isinstance(b, dict) and _text(b.get("text"))
    ]

    return MeetingReport(
        title=_text(raw.get("title"), 200) or meeting.title,
        overview=_text(raw.get("overview")) or "No summary was produced.",
        key_points=[_text(p, 500) for p in raw.get("key_points") or [] if _text(p)],
        topics=topics,
        decisions=decisions,
        action_items=items,
        blockers=blockers,
        next_steps=[_text(s, 500) for s in raw.get("next_steps") or [] if _text(s)],
        next_meeting=_text(raw.get("next_meeting"), 200),
        generated_by=model,
        notes=notes,
    )
