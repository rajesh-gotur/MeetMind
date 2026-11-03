"""Shared helpers for every exporter: one place for formatting and grouping rules."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.schemas import ExportRequest, ScreenshotInfo, Topic

BRAND = "MeetMind"
ACCENT_HEX = "0F7A55"  # deep jade, matches the extension's accent
ACCENT_SOFT_HEX = "E7F5EE"
INK_HEX = "1F1D1A"
MUTED_HEX = "6F6A62"


def clock(seconds: float | None) -> str:
    """Seconds -> HH:MM:SS (meeting-relative)."""
    total = max(0, int(seconds or 0))
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def minutes_label(seconds: float) -> str:
    minutes = round(seconds / 60)
    if minutes < 1:
        return "under 1 min"
    hours, mins = divmod(minutes, 60)
    return f"{hours} h {mins} min" if hours else f"{mins} min"


def slugify(text: str, fallback: str = "meeting") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48]
    return slug or fallback


@dataclass
class MeetingView:
    """Everything an exporter needs, computed once."""

    request: ExportRequest
    start: datetime
    end: datetime
    attendees: list[str]
    talk_time: dict[str, float]
    shots_by_topic: dict[int, list[ScreenshotInfo]] = field(default_factory=dict)
    loose_shots: list[ScreenshotInfo] = field(default_factory=list)

    @property
    def report(self):  # noqa: ANN201 - convenience accessor
        return self.request.report

    @property
    def title(self) -> str:
        return self.report.title or self.request.meeting.title

    @property
    def date_label(self) -> str:
        return self.start.strftime("%A, %d %B %Y")

    @property
    def time_label(self) -> str:
        offset = self.start.strftime("%z")  # e.g. +0530
        zone = f"(UTC{offset[:3]}:{offset[3:]})" if offset else ""
        return f"{self.start.strftime('%H:%M')} – {self.end.strftime('%H:%M')} {zone}".strip()

    @property
    def file_stem(self) -> str:
        return f"{self.start.strftime('%Y-%m-%d')}_{slugify(self.title)}"

    def shot_time_label(self, shot: ScreenshotInfo) -> str:
        return (self.start + timedelta(seconds=shot.time)).strftime("%H:%M:%S")


def build_view(request: ExportRequest) -> MeetingView:
    meeting = request.meeting
    start = meeting.started_at
    end = start + timedelta(seconds=meeting.duration_seconds)

    talk: dict[str, float] = {}
    for turn in request.turns:
        talk[turn.speaker] = talk.get(turn.speaker, 0.0) + max(turn.end - turn.start, 0.0)
    attendees = list(dict.fromkeys([*request.participants, *talk.keys()]))
    attendees = [a for a in attendees if a and a != "Unknown speaker"]

    view = MeetingView(request=request, start=start, end=end, attendees=attendees, talk_time=talk)
    _place_screenshots(view, request.report.topics, request.screenshots)
    return view


def _place_screenshots(view: MeetingView, topics: list[Topic], shots: list[ScreenshotInfo]) -> None:
    """Each screenshot goes under the topic that was being discussed when it was taken."""
    for shot in sorted(shots, key=lambda s: s.time):
        index = next((i for i, t in enumerate(topics) if t.start <= shot.time <= max(t.end, t.start)), None)
        if index is None and topics:
            # Between topics: attach to the latest topic that had started.
            started = [i for i, t in enumerate(topics) if t.start <= shot.time]
            index = started[-1] if started else 0
        if index is None:
            view.loose_shots.append(shot)
        else:
            view.shots_by_topic.setdefault(index, []).append(shot)


QUALITY_ROWS = (
    ("overall", "Overall"),
    ("capture", "Capture (meeting recorded)"),
    ("words", "Words (confirmed by 2+ ears)"),
    ("speakers", "Speakers (confirmed names)"),
    ("notes", "Notes (tasks backed by a quote)"),
)


def quality_rows(view: MeetingView) -> list[tuple[str, str]]:
    """Rows for the "Quality" section; empty when there is no score or it is switched off."""
    from app.config import get_settings

    quality = view.report.quality
    if quality is None or not get_settings().quality.show_in_files:
        return []
    rows = [(label, f"{getattr(quality, key)} / 100") for key, label in QUALITY_ROWS]
    if quality.ears:
        rows.append(("Ears used", ", ".join(quality.ears)))
    rows.append(("Words corrected", str(quality.corrected_words)))
    return rows


def transcript_text(view: MeetingView) -> str:
    request = view.request
    header = [
        f"{BRAND} transcript",
        f"Meeting: {view.title}",
        f"Date: {view.date_label}, {view.time_label}",
        f"Duration: {minutes_label(request.meeting.duration_seconds)}",
        f"Attendees: {', '.join(view.attendees) or 'Not detected'}",
        "",
    ]
    body = [f"[{clock(t.start)}] {t.speaker}: {t.text}" for t in request.turns]
    return "\n".join([*header, *body, ""])
