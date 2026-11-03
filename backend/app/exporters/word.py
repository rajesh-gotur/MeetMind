"""Meeting_Report.docx — a clean, styled Word report with screenshots."""

from __future__ import annotations

import io

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor
from docx.table import Table

from app.exporters.common import (
    ACCENT_HEX,
    ACCENT_SOFT_HEX,
    BRAND,
    INK_HEX,
    MUTED_HEX,
    MeetingView,
    clock,
    minutes_label,
    quality_rows,
)

ACCENT = RGBColor.from_string(ACCENT_HEX)
INK = RGBColor.from_string(INK_HEX)
MUTED = RGBColor.from_string(MUTED_HEX)
PRIORITY_COLOURS = {"High": "C2410C", "Medium": "A16207", "Low": "4D7C0F"}
FONT = "Calibri"


def _shade(cell, hex_colour: str) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    shading = OxmlElement("w:shd")
    shading.set(qn("w:val"), "clear")
    shading.set(qn("w:color"), "auto")
    shading.set(qn("w:fill"), hex_colour)
    tc_pr.append(shading)


def _borders(table: Table, colour: str = "E8E3DA") -> None:
    tbl_pr = table._tbl.tblPr
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        element = OxmlElement(f"w:{edge}")
        element.set(qn("w:val"), "single")
        element.set(qn("w:sz"), "4")
        element.set(qn("w:color"), colour)
        borders.append(element)
    tbl_pr.append(borders)


def _run(paragraph, text: str, *, bold=False, size=None, colour=None, italic=False):  # noqa: ANN001, ANN202
    run = paragraph.add_run(text)
    run.bold = bold
    run.italic = italic
    if size:
        run.font.size = Pt(size)
    if colour is not None:
        run.font.color.rgb = colour
    return run


def _heading(document, text: str, level: int = 1) -> None:  # noqa: ANN001
    paragraph = document.add_heading(level=level)
    _run(paragraph, text, bold=True, size=15 if level == 1 else 12.5, colour=ACCENT if level == 1 else INK)
    paragraph.paragraph_format.space_before = Pt(14 if level == 1 else 10)
    paragraph.paragraph_format.space_after = Pt(4)


def _setup_styles(document) -> None:  # noqa: ANN001
    normal = document.styles["Normal"]
    normal.font.name = FONT
    normal.font.size = Pt(10.5)
    normal.font.color.rgb = INK
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), FONT)
    for style_name in ("Heading 1", "Heading 2", "Title"):
        style = document.styles[style_name]
        style.font.name = FONT
        style.font.color.rgb = INK
    for section in document.sections:
        section.top_margin = section.bottom_margin = Cm(2)
        section.left_margin = section.right_margin = Cm(2.2)


def _table(document, headers: list[str], rows: list[list[str]], widths: list[float]) -> Table:  # noqa: ANN001
    table = document.add_table(rows=1, cols=len(headers))
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    _borders(table)
    for i, header in enumerate(headers):
        cell = table.rows[0].cells[i]
        _shade(cell, ACCENT_HEX)
        _run(cell.paragraphs[0], header, bold=True, size=9.5, colour=RGBColor(0xFF, 0xFF, 0xFF))
    for row_index, values in enumerate(rows):
        cells = table.add_row().cells
        for i, value in enumerate(values):
            _run(cells[i].paragraphs[0], value, size=9.5)
            if row_index % 2:
                _shade(cells[i], "FAF8F5")
    # Word and LibreOffice both respect widths only when autofit is off and every cell is set.
    table.autofit = False
    for i, width in enumerate(widths):
        table.columns[i].width = Cm(width)
        for cell in table.columns[i].cells:
            cell.width = Cm(width)
    return table


def build_word(view: MeetingView, images: dict[str, bytes]) -> bytes:
    report = view.report
    meeting = view.request.meeting
    document = Document()
    _setup_styles(document)

    # ---- cover block
    eyebrow = document.add_paragraph()
    _run(eyebrow, f"{BRAND.upper()} · MEETING REPORT", bold=True, size=9, colour=ACCENT)
    title = document.add_paragraph()
    _run(title, view.title, bold=True, size=24, colour=INK)
    title.paragraph_format.space_after = Pt(2)
    subtitle = document.add_paragraph()
    _run(
        subtitle,
        f"{view.date_label} · {view.time_label} · {minutes_label(meeting.duration_seconds)}",
        size=10.5,
        colour=MUTED,
    )

    facts = document.add_table(rows=2, cols=4)
    _borders(facts, "FFFFFF")
    labels = ["Attendees", "Action items", "Decisions", "Recorded by"]
    values = [
        str(len(view.attendees)),
        str(len(report.action_items)),
        str(len(report.decisions)),
        meeting.recorded_by or "—",
    ]
    for i, (label, value) in enumerate(zip(labels, values, strict=True)):
        _shade(facts.rows[0].cells[i], ACCENT_SOFT_HEX)
        _shade(facts.rows[1].cells[i], ACCENT_SOFT_HEX)
        _run(facts.rows[0].cells[i].paragraphs[0], value, bold=True, size=16, colour=ACCENT)
        _run(facts.rows[1].cells[i].paragraphs[0], label, size=9, colour=MUTED)

    # ---- summary
    _heading(document, "Summary")
    document.add_paragraph(report.overview)
    if report.key_points:
        _heading(document, "Key points", 2)
        for point in report.key_points:
            document.add_paragraph(point, style="List Bullet")

    # ---- attendees
    _heading(document, "Attendees")
    if view.attendees:
        _table(
            document,
            ["Name", "Talk time"],
            [[name, minutes_label(view.talk_time.get(name, 0))] for name in view.attendees],
            [11, 5],
        )
    else:
        document.add_paragraph("Attendees were not detected.")

    # ---- action items
    _heading(document, "Action items")
    if report.action_items:
        table = _table(
            document,
            ["ID", "Task", "Owner", "Due", "Priority"],
            [
                [
                    item.id,
                    item.task + ("" if item.verified else "  (please check)"),
                    item.owner,
                    item.due_date.strftime("%d %b %Y") if item.due_date else (item.due_text or "—"),
                    item.priority,
                ]
                for item in report.action_items
            ],
            [1.6, 7.6, 3.2, 2.6, 1.8],
        )
        for row, item in zip(table.rows[1:], report.action_items, strict=True):
            for run in row.cells[4].paragraphs[0].runs:
                run.font.color.rgb = RGBColor.from_string(PRIORITY_COLOURS[item.priority])
                run.bold = True
        note = document.add_paragraph()
        _run(
            note,
            "Each task was checked against what was said; times link back to the transcript.",
            italic=True,
            size=8.5,
            colour=MUTED,
        )
    else:
        document.add_paragraph("No action items were assigned.")

    # ---- decisions and blockers
    _heading(document, "Decisions")
    if report.decisions:
        for decision in report.decisions:
            paragraph = document.add_paragraph(style="List Bullet")
            _run(paragraph, decision.text)
            if decision.made_by:
                _run(paragraph, f" — {decision.made_by}", colour=MUTED)
    else:
        document.add_paragraph("No decisions were recorded.")
    if report.blockers:
        _heading(document, "Blockers and risks")
        for blocker in report.blockers:
            paragraph = document.add_paragraph(style="List Bullet")
            _run(paragraph, blocker.text)
            if blocker.raised_by:
                _run(paragraph, f" — raised by {blocker.raised_by}", colour=MUTED)

    # ---- discussion with screenshots
    _heading(document, "Discussion")
    for index, topic in enumerate(report.topics):
        _heading(document, f"{index + 1}. {topic.title}", 2)
        meta = document.add_paragraph()
        speakers = f" · {', '.join(topic.speakers)}" if topic.speakers else ""
        _run(meta, f"{clock(topic.start)} – {clock(topic.end)}{speakers}", size=9, colour=MUTED)
        document.add_paragraph(topic.summary)
        _pictures(document, view, view.shots_by_topic.get(index, []), images)
    if not report.topics:
        document.add_paragraph("No separate topics were identified.")
    if view.loose_shots:
        _heading(document, "Screens shown", 2)
        _pictures(document, view, view.loose_shots, images)

    if report.next_steps or report.next_meeting:
        _heading(document, "Next steps")
        for step in report.next_steps:
            document.add_paragraph(step, style="List Bullet")
        if report.next_meeting:
            paragraph = document.add_paragraph()
            _run(paragraph, "Next meeting: ", bold=True)
            _run(paragraph, report.next_meeting)

    # ---- quality (estimated)
    rows = quality_rows(view)
    if rows:
        _heading(document, "Quality (estimated)")
        _table(document, ["Check", "Score"], [[label, value] for label, value in rows], [3.6, 2.6])
        for note in report.quality.fallbacks if report.quality else []:
            document.add_paragraph(note, style="List Bullet")

    # ---- transcript appendix
    document.add_page_break()
    _heading(document, "Appendix — Full transcript")
    for turn in view.request.turns:
        paragraph = document.add_paragraph()
        paragraph.paragraph_format.space_after = Pt(3)
        _run(paragraph, f"[{clock(turn.start)}] ", size=9, colour=MUTED)
        _run(paragraph, f"{turn.speaker}: ", bold=True, size=9.5, colour=ACCENT)
        _run(paragraph, turn.text, size=9.5)

    footer = document.sections[0].footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _run(footer, f"Generated by {BRAND} · {view.title}", size=8, colour=MUTED)

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _pictures(document, view: MeetingView, shots, images: dict[str, bytes]) -> None:  # noqa: ANN001
    for shot in shots:
        data = images.get(shot.name)
        if not data:
            continue
        document.add_picture(io.BytesIO(data), width=Cm(15.5))
        document.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
        caption = document.add_paragraph()
        caption.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _run(
            caption,
            f"Screen at {view.shot_time_label(shot)} ({clock(shot.time)} into the meeting)",
            italic=True,
            size=8.5,
            colour=MUTED,
        )
