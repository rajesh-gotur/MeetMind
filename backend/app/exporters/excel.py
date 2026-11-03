"""Action_Tracker.xlsx — a task tracker the team keeps updating after the meeting."""

from __future__ import annotations

import io

from openpyxl import Workbook
from openpyxl.formatting.rule import CellIsRule, FormulaRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.worksheet.table import Table, TableStyleInfo
from openpyxl.worksheet.worksheet import Worksheet

from app.exporters.common import ACCENT_HEX, ACCENT_SOFT_HEX, BRAND, MUTED_HEX, MeetingView, clock, minutes_label

FONT_NAME = "Calibri"
STATUSES = ["Not started", "In progress", "Blocked", "Done"]
PRIORITIES = ["High", "Medium", "Low"]
HEADER_ROW = 4
THIN = Side(style="thin", color="E8E3DA")

STATUS_FILLS = {
    "Done": ("DCFCE7", "166534"),
    "In progress": ("FEF3C7", "92400E"),
    "Blocked": ("FEE2E2", "991B1B"),
    "Not started": ("F4F1EC", "57534E"),
}
PRIORITY_FONTS = {"High": "C2410C", "Medium": "A16207", "Low": "4D7C0F"}


def _font(**kwargs) -> Font:  # noqa: ANN003
    return Font(name=FONT_NAME, **kwargs)


def _print_setup(sheet: Worksheet) -> None:
    """Landscape, fit every column on one page width when printed."""
    sheet.page_setup.orientation = "landscape"
    sheet.page_setup.paperSize = sheet.PAPERSIZE_A4
    sheet.page_setup.fitToWidth = 1
    sheet.page_setup.fitToHeight = 0
    sheet.sheet_properties.pageSetUpPr.fitToPage = True
    sheet.print_options.horizontalCentered = True
    sheet.page_margins.left = sheet.page_margins.right = 0.4


def _title_block(sheet: Worksheet, title: str, subtitle: str, width: int) -> None:
    sheet["A1"] = title
    sheet["A1"].font = _font(size=16, bold=True, color="1F1D1A")
    sheet["A2"] = subtitle
    sheet["A2"].font = _font(size=10, color=MUTED_HEX)
    sheet.row_dimensions[1].height = 26
    for row in (1, 2):
        sheet.merge_cells(start_row=row, start_column=1, end_row=row, end_column=width)


def _header(sheet: Worksheet, row: int, headers: list[str], widths: list[int]) -> None:
    for column, (name, width) in enumerate(zip(headers, widths, strict=True), start=1):
        cell = sheet.cell(row=row, column=column, value=name)
        cell.font = _font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor=ACCENT_HEX)
        cell.alignment = Alignment(vertical="center", wrap_text=True)
        sheet.column_dimensions[get_column_letter(column)].width = width
    sheet.row_dimensions[row].height = 22


def _body_style(sheet: Worksheet, first_row: int, last_row: int, columns: int) -> None:
    for row in sheet.iter_rows(min_row=first_row, max_row=last_row, max_col=columns):
        for cell in row:
            if cell.font.name != FONT_NAME:
                cell.font = _font(bold=cell.font.bold, italic=cell.font.italic, color=cell.font.color)
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            cell.border = Border(top=THIN, bottom=THIN, left=THIN, right=THIN)


def _tracker(sheet: Worksheet, view: MeetingView) -> None:
    report = view.report
    headers = [
        "Task ID",
        "Task",
        "Owner",
        "Assigned by",
        "Due date",
        "Priority",
        "Status",
        "Said at",
        "What was said",
        "Check",
        "Remarks",
    ]
    widths = [10, 46, 20, 18, 13, 11, 14, 11, 46, 14, 28]
    _title_block(
        sheet,
        f"{view.title} — Action tracker",
        f"{view.date_label} · {view.time_label} · {len(report.action_items)} tasks · "
        "update Status and Remarks as work progresses",
        len(headers),
    )
    _header(sheet, HEADER_ROW, headers, widths)

    for offset, item in enumerate(report.action_items, start=1):
        row = HEADER_ROW + offset
        values = [
            item.id,
            item.task,
            item.owner,
            item.assigned_by,
            item.due_date,
            item.priority,
            "Not started",
            clock(item.time),
            item.evidence,
            "OK" if item.verified else "Please check",
            item.due_text if item.due_text and not item.due_date else "",
        ]
        for column, value in enumerate(values, start=1):
            sheet.cell(row=row, column=column, value=value)
        sheet.cell(row=row, column=5).number_format = "DD-MMM-YYYY"
        sheet.cell(row=row, column=6).font = _font(bold=True, color=PRIORITY_FONTS[item.priority])
        sheet.cell(row=row, column=9).font = _font(italic=True, color=MUTED_HEX)
        if not item.verified:
            sheet.cell(row=row, column=10).font = _font(bold=True, color="B45309")

    last = HEADER_ROW + max(len(report.action_items), 1)
    if not report.action_items:
        sheet.cell(row=last, column=2, value="No action items were assigned in this meeting.")
    _body_style(sheet, HEADER_ROW + 1, last, len(headers))

    # Dropdowns for the columns people keep updating (with room for new rows).
    end = last + 200
    priority = DataValidation(type="list", formula1=f'"{",".join(PRIORITIES)}"', allow_blank=True)
    status = DataValidation(type="list", formula1=f'"{",".join(STATUSES)}"', allow_blank=True)
    for validation, letter in ((priority, "F"), (status, "G")):
        validation.error = "Pick a value from the list."
        validation.showErrorMessage = True
        sheet.add_data_validation(validation)
        validation.add(f"{letter}{HEADER_ROW + 1}:{letter}{end}")

    for name, (fill, font) in STATUS_FILLS.items():
        sheet.conditional_formatting.add(
            f"G{HEADER_ROW + 1}:G{end}",
            CellIsRule(
                operator="equal",
                formula=[f'"{name}"'],
                fill=PatternFill("solid", fgColor=fill),
                font=_font(bold=True, color=font),
            ),
        )
    # Overdue: a past due date on a task that is not done turns red.
    sheet.conditional_formatting.add(
        f"E{HEADER_ROW + 1}:E{end}",
        FormulaRule(
            formula=[f'AND(E{HEADER_ROW + 1}<>"",E{HEADER_ROW + 1}<TODAY(),G{HEADER_ROW + 1}<>"Done")'],
            fill=PatternFill("solid", fgColor="FEE2E2"),
            font=_font(bold=True, color="991B1B"),
        ),
    )

    if report.action_items:
        table = Table(displayName="ActionItems", ref=f"A{HEADER_ROW}:{get_column_letter(len(headers))}{last}")
        table.tableStyleInfo = TableStyleInfo(name="TableStyleLight9", showRowStripes=True)
        sheet.add_table(table)
    sheet.freeze_panes = f"C{HEADER_ROW + 1}"
    sheet.sheet_view.zoomScale = 110


def _decisions(sheet: Worksheet, view: MeetingView) -> None:
    report = view.report
    headers = ["Type", "Details", "Who", "Said at"]
    _title_block(sheet, "Decisions and blockers", view.title, len(headers))
    _header(sheet, HEADER_ROW, headers, [14, 70, 22, 11])
    rows = [("Decision", d.text, d.made_by, clock(d.time) if d.time is not None else "") for d in report.decisions]
    rows += [("Blocker", b.text, b.raised_by, clock(b.time) if b.time is not None else "") for b in report.blockers]
    if not rows:
        rows = [("—", "No decisions or blockers were recorded.", "", "")]
    for offset, values in enumerate(rows, start=1):
        for column, value in enumerate(values, start=1):
            sheet.cell(row=HEADER_ROW + offset, column=column, value=value)
        kind = sheet.cell(row=HEADER_ROW + offset, column=1)
        kind.font = _font(bold=True, color=ACCENT_HEX if values[0] == "Decision" else "B91C1C")
    _body_style(sheet, HEADER_ROW + 1, HEADER_ROW + len(rows), len(headers))
    sheet.freeze_panes = f"A{HEADER_ROW + 1}"


def _info(sheet: Worksheet, view: MeetingView) -> None:
    meeting = view.request.meeting
    _title_block(sheet, "Meeting information", f"Prepared by {BRAND}", 2)
    facts = [
        ("Meeting", view.title),
        ("Date", view.date_label),
        ("Time", view.time_label),
        ("Duration", minutes_label(meeting.duration_seconds)),
        ("Meeting code", meeting.code or "—"),
        ("Recorded by", meeting.recorded_by or "—"),
        ("Summary", view.report.overview),
    ]
    for offset, (label, value) in enumerate(facts, start=HEADER_ROW):
        sheet.cell(row=offset, column=1, value=label).font = _font(bold=True, color=MUTED_HEX)
        sheet.cell(row=offset, column=2, value=value).alignment = Alignment(wrap_text=True, vertical="top")
    row = HEADER_ROW + len(facts) + 1
    _header(sheet, row, ["Attendee", "Talk time"], [18, 70])
    for offset, name in enumerate(view.attendees, start=1):
        sheet.cell(row=row + offset, column=1, value=name)
        sheet.cell(row=row + offset, column=2, value=minutes_label(view.talk_time.get(name, 0)))
    for column in ("A", "B"):
        for cell in sheet[column][HEADER_ROW - 1 : row - 1]:
            cell.fill = PatternFill("solid", fgColor=ACCENT_SOFT_HEX)


def build_excel(view: MeetingView) -> bytes:
    workbook = Workbook()
    tracker = workbook.active
    tracker.title = "Action Tracker"
    _tracker(tracker, view)
    _decisions(workbook.create_sheet("Decisions & Blockers"), view)
    _info(workbook.create_sheet("Meeting Info"), view)
    tracker.sheet_properties.tabColor = ACCENT_HEX
    for sheet in workbook.worksheets:
        _print_setup(sheet)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
