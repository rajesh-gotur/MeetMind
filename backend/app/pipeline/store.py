"""Saved meetings (backend/meetings/<id>/), so nothing is lost and notes can be made again.

Each folder holds the audio pieces, what every ear heard, the captions, the
who-is-talking timeline, the final transcript and the notes. Folders older than
storage.keep_days are deleted automatically when the server starts and after
every meeting.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SAFE = re.compile(r"[^A-Za-z0-9_-]+")


class MeetingFolder:
    def __init__(self, path: Path) -> None:
        self.path = path

    @property
    def id(self) -> str:
        return self.path.name

    def save_piece(self, index: int, audio: bytes) -> None:
        self._write_bytes(f"piece-{index:03d}.webm", audio)

    def save_json(self, name: str, data: Any) -> None:
        self._write_bytes(f"{name}.json", json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"))

    def load_json(self, name: str, default: Any = None) -> Any:
        try:
            return json.loads((self.path / f"{name}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    def _write_bytes(self, name: str, data: bytes) -> None:
        try:
            self.path.mkdir(parents=True, exist_ok=True)
            tmp = self.path / f".{name}.tmp"
            tmp.write_bytes(data)
            tmp.replace(self.path / name)  # never leave a half-written file
        except OSError as exc:
            logger.warning("Could not save %s for meeting %s: %s", name, self.id, exc)


class MeetingStore:
    def __init__(self, root: Path, keep_days: int, enabled: bool = True) -> None:
        self.root = root
        self.keep_days = keep_days
        self.enabled = enabled

    def create(self, code: str) -> MeetingFolder | None:
        if not self.enabled:
            return None
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return MeetingFolder(self.root / f"{stamp}_{_SAFE.sub('-', code)[:40] or 'meeting'}")

    def open(self, meeting_id: str) -> MeetingFolder | None:
        path = self.root / _SAFE.sub("-", meeting_id)
        return MeetingFolder(path) if path.is_dir() else None

    def cleanup(self) -> int:
        """Deletes meetings older than keep_days. Returns how many were deleted."""
        if not self.root.is_dir():
            return 0
        limit = time.time() - self.keep_days * 86400
        removed = 0
        for folder in self.root.iterdir():
            try:
                if folder.is_dir() and folder.stat().st_mtime < limit:
                    shutil.rmtree(folder, ignore_errors=True)
                    removed += 1
            except OSError:
                continue
        if removed:
            logger.info("Deleted %s saved meeting(s) older than %s days", removed, self.keep_days)
        return removed
