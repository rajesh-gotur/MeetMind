"""Packs every meeting file into one zip, or returns a single file."""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from typing import Literal

from app.exporters.common import MeetingView, transcript_text
from app.exporters.excel import build_excel
from app.exporters.markdown import SHOTS_DIR, build_mom, build_readme
from app.exporters.word import build_word

FileKind = Literal["zip", "docx", "xlsx", "mom", "readme", "transcript"]


@dataclass(frozen=True)
class BuiltFile:
    filename: str
    media_type: str
    content: bytes


FILE_NAMES = {
    "docx": "Meeting_Report.docx",
    "xlsx": "Action_Tracker.xlsx",
    "mom": "MOM.md",
    "readme": "README.md",
    "transcript": "Transcript.txt",
}
DESCRIPTIONS = {
    "Meeting_Report.docx": "Full report: summary, tasks, decisions, discussion with screenshots, transcript",
    "Action_Tracker.xlsx": "Task tracker with owners, due dates, priority and status dropdowns",
    "MOM.md": "Minutes of meeting",
    "Transcript.txt": "Word-for-word transcript with speaker names and times",
}
MEDIA = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "mom": "text/markdown; charset=utf-8",
    "readme": "text/markdown; charset=utf-8",
    "transcript": "text/plain; charset=utf-8",
}


def _files_table(view: MeetingView, images: dict[str, bytes]) -> dict[str, str]:
    files = dict(DESCRIPTIONS)
    if images:
        files[f"{SHOTS_DIR}/"] = f"{len(images)} screenshots taken when the shared screen changed"
    return files


def build_one(view: MeetingView, kind: FileKind, images: dict[str, bytes]) -> BuiltFile:
    if kind == "zip":
        return build_zip(view, images)
    builders = {
        "docx": lambda: build_word(view, images),
        "xlsx": lambda: build_excel(view),
        "mom": lambda: build_mom(view).encode(),
        "readme": lambda: build_readme(view, _files_table(view, images)).encode(),
        "transcript": lambda: transcript_text(view).encode(),
    }
    return BuiltFile(f"{view.file_stem}_{FILE_NAMES[kind]}", MEDIA[kind], builders[kind]())


def build_zip(view: MeetingView, images: dict[str, bytes]) -> BuiltFile:
    folder = view.file_stem
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{folder}/README.md", build_readme(view, _files_table(view, images)))
        archive.writestr(f"{folder}/MOM.md", build_mom(view))
        archive.writestr(f"{folder}/Meeting_Report.docx", build_word(view, images))
        archive.writestr(f"{folder}/Action_Tracker.xlsx", build_excel(view))
        archive.writestr(f"{folder}/Transcript.txt", transcript_text(view))
        for name, data in images.items():
            # Images are already compressed; storing them avoids wasted CPU.
            archive.writestr(zipfile.ZipInfo(f"{folder}/{SHOTS_DIR}/{name}"), data, compress_type=zipfile.ZIP_STORED)
    return BuiltFile(f"MeetMind_{folder}.zip", "application/zip", buffer.getvalue())
