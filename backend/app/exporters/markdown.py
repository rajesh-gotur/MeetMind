"""MOM.md (minutes of meeting) and README.md."""

from __future__ import annotations

from app.exporters.common import BRAND, MeetingView, clock, minutes_label, quality_rows
from app.schemas import ActionItem

SHOTS_DIR = "screenshots"


def _cell(text: str) -> str:
    """Makes text safe inside a Markdown table cell."""
    return " ".join(text.split()).replace("|", "\\|")


def _due(item: ActionItem) -> str:
    if item.due_date:
        return item.due_date.strftime("%d %b %Y")
    return item.due_text or "—"


def _task_rows(items: list[ActionItem]) -> list[str]:
    rows = ["| ID | Task | Owner | Due | Priority | Said at |", "| --- | --- | --- | --- | --- | --- |"]
    for item in items:
        check = "" if item.verified else " ⚠ _please check_"
        rows.append(
            f"| {item.id} | {_cell(item.task)}{check} | {_cell(item.owner)} | {_due(item)} | "
            f"{item.priority} | {clock(item.time)} |"
        )
    return rows


def build_mom(view: MeetingView) -> str:
    report = view.report
    meeting = view.request.meeting
    out: list[str] = [
        f"# Minutes of Meeting — {view.title}",
        "",
        "| | |",
        "| --- | --- |",
        f"| **Date** | {view.date_label} |",
        f"| **Time** | {view.time_label} |",
        f"| **Duration** | {minutes_label(meeting.duration_seconds)} |",
    ]
    if meeting.code:
        out.append(f"| **Meeting code** | {meeting.code} |")
    if meeting.recorded_by:
        out.append(f"| **Minutes taken by** | {_cell(meeting.recorded_by)} (recorded with {BRAND}) |")

    out += ["", "## Attendees", ""]
    if view.attendees:
        out += [f"- {name} — spoke about {minutes_label(view.talk_time.get(name, 0))}" for name in view.attendees]
    else:
        out.append("- Not detected")

    out += ["", "## Summary", "", report.overview, ""]
    if report.key_points:
        out += ["### Key points", "", *[f"- {p}" for p in report.key_points], ""]

    out += ["## Discussion", ""]
    if report.topics:
        for index, topic in enumerate(report.topics):
            out += [f"### {index + 1}. {topic.title}", f"_{clock(topic.start)} – {clock(topic.end)}_", ""]
            if topic.speakers:
                out += [f"**Speakers:** {', '.join(topic.speakers)}", ""]
            out += [topic.summary, ""]
            for shot in view.shots_by_topic.get(index, []):
                out += [f"![Screen at {view.shot_time_label(shot)}]({SHOTS_DIR}/{shot.name})", ""]
    else:
        out += ["No separate topics were identified.", ""]
    if view.loose_shots:
        out += ["### Screens shown", ""]
        out += [f"![Screen at {view.shot_time_label(s)}]({SHOTS_DIR}/{s.name})" for s in view.loose_shots]
        out.append("")

    out += ["## Decisions", ""]
    out += (
        [f"- {d.text}" + (f" — _{d.made_by}_" if d.made_by else "") for d in report.decisions]
        if report.decisions
        else ["- No decisions were recorded."]
    )

    out += ["", "## Action items", ""]
    out += _task_rows(report.action_items) if report.action_items else ["No action items were assigned."]

    if report.blockers:
        out += ["", "## Blockers and risks", ""]
        out += [f"- {b.text}" + (f" — raised by {b.raised_by}" if b.raised_by else "") for b in report.blockers]
    if report.next_steps:
        out += ["", "## Next steps", "", *[f"- {s}" for s in report.next_steps]]
    out += ["", "## Next meeting", "", report.next_meeting or "Not scheduled in this meeting."]
    if report.notes:
        out += ["", "> **Note:** " + " ".join(report.notes)]
    rows = quality_rows(view)
    if rows:
        out += ["", "## Quality (estimated)", "", "| Check | Score |", "| --- | --- |"]
        out += [f"| {label} | {_cell(value)} |" for label, value in rows]
        if report.quality and report.quality.fallbacks:
            out += ["", *[f"- {_cell(note)}" for note in report.quality.fallbacks]]
    out += [
        "",
        "---",
        f"_Prepared automatically by {BRAND} from the meeting recording. Please review before sharing._",
        "",
    ]
    return "\n".join(out)


def build_readme(view: MeetingView, files: dict[str, str]) -> str:
    report = view.report
    meeting = view.request.meeting
    out: list[str] = [
        f"# {view.title}",
        "",
        f"> {report.overview}",
        "",
        f"**Date:** {view.date_label} · **Duration:** {minutes_label(meeting.duration_seconds)} · "
        f"**Attendees:** {', '.join(view.attendees) or 'Not detected'}",
        "",
    ]
    if report.key_points:
        out += ["## Key outcomes", "", *[f"- {p}" for p in report.key_points], ""]
    if report.decisions:
        out += ["## Decisions", "", *[f"- {d.text}" for d in report.decisions], ""]
    out += ["## Tasks", ""]
    if report.action_items:
        for item in report.action_items:
            due = f" — due {_due(item)}" if item.due_date or item.due_text else ""
            flag = " _(please check)_" if not item.verified else ""
            out.append(f"- [ ] **{item.id}** {item.task} — @{item.owner}{due} · {item.priority}{flag}")
    else:
        out.append("No tasks were assigned in this meeting.")
    out += ["", "## Files in this folder", "", "| File | What it is |", "| --- | --- |"]
    out += [f"| `{name}` | {description} |" for name, description in files.items()]
    out += ["", "---", f"_Generated by {BRAND}._", ""]
    return "\n".join(out)
