"""Live state of the bot. The bot thread writes it; API requests read it."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("meetmind.bot")

MAX_CAPTIONS = 80
MAX_EVENTS = 40
MAX_LEVELS = 48

ACTIVE_PHASES = {"starting", "joining", "waiting", "recording", "finishing", "writing"}


@dataclass
class _State:
    phase: str = "idle"  # idle, starting, joining, waiting, recording, finishing, writing, done, error
    meeting_url: str = ""
    meeting_code: str = ""
    title: str = ""
    bot_name: str = ""
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None  # epoch seconds when recording started
    ended_at: float | None = None
    levels: list[float] = field(default_factory=list)
    people: dict[str, dict[str, Any]] = field(default_factory=dict)
    captions: list[dict[str, Any]] = field(default_factory=list)
    captions_final: bool = False
    captions_on: bool = False
    tracks: int = 0
    pieces: list[dict[str, Any]] = field(default_factory=list)
    engine: str = ""
    meeting_id: str = ""  # saved meeting folder (for "Create notes again")
    health: dict[str, Any] = field(default_factory=dict)  # live watchdog result: score, in_call, sound, captions
    report: dict[str, Any] | None = None
    error: str = ""
    warnings: list[str] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)


class BotStatus:
    """Thread-safe holder for one bot run."""

    def __init__(self) -> None:
        self._state = _State()
        self._lock = threading.Lock()
        self.stop_requested = threading.Event()
        self.stop_reason = "button"  # "button" or "you_left"

    # ---- writing (bot thread)

    def update(self, **changes: Any) -> None:
        with self._lock:
            for key, value in changes.items():
                setattr(self._state, key, value)

    def event(self, text: str) -> None:
        logger.info("%s", text)
        with self._lock:
            self._state.events = [*self._state.events, {"t": time.time(), "text": text}][-MAX_EVENTS:]

    def warn(self, text: str) -> None:
        logger.warning("%s", text)
        with self._lock:
            if text not in self._state.warnings:
                self._state.warnings.append(text)
            self._state.events = [*self._state.events, {"t": time.time(), "text": text}][-MAX_EVENTS:]

    def fail(self, message: str) -> None:
        logger.error("Bot stopped: %s", message)
        self.update(phase="error", error=message, ended_at=self.get("ended_at") or time.time())
        with self._lock:
            self._state.events = [*self._state.events, {"t": time.time(), "text": message}][-MAX_EVENTS:]

    def add_levels(self, values: list[float]) -> None:
        with self._lock:
            self._state.levels = (self._state.levels + values)[-MAX_LEVELS:]

    def set_people(self, present: list[str], speaking: set[str]) -> None:
        with self._lock:
            now = time.time()
            for name in present:
                self._state.people.setdefault(name, {"name": name, "words": 0, "last": 0.0})
            for name in speaking:
                self._state.people.setdefault(name, {"name": name, "words": 0, "last": 0.0})["last"] = now
            for person in self._state.people.values():
                person["speaking"] = now - person.get("last", 0.0) < 2.5
                person["present"] = person["name"] in present or person["speaking"]

    def add_words(self, name: str, count: int) -> None:
        with self._lock:
            person = self._state.people.setdefault(name, {"name": name, "words": 0, "last": 0.0})
            person["words"] = person.get("words", 0) + max(0, count)

    def set_captions(self, lines: list[dict[str, Any]], final: bool = False) -> None:
        with self._lock:
            self._state.captions = lines if final else lines[-MAX_CAPTIONS:]
            self._state.captions_final = final

    def piece(self, index: int, **changes: Any) -> None:
        with self._lock:
            for piece in self._state.pieces:
                if piece["index"] == index:
                    piece.update(changes)
                    return
            self._state.pieces.append({"index": index, **changes})

    # ---- reading (API)

    def get(self, key: str) -> Any:
        with self._lock:
            return getattr(self._state, key)

    @property
    def active(self) -> bool:
        return self.get("phase") in ACTIVE_PHASES

    def snapshot(self, captions_tail: int | None = None) -> dict[str, Any]:
        with self._lock:
            data = dict(self._state.__dict__)
            data["people"] = sorted(
                ({k: v for k, v in p.items() if k != "last"} for p in self._state.people.values()),
                key=lambda p: (-p.get("words", 0), p["name"]),
            )
            data["pieces"] = [dict(p) for p in self._state.pieces]
            captions = [dict(c) for c in self._state.captions]
            data["captions"] = captions[-captions_tail:] if captions_tail else captions
            data["now"] = time.time()
            return data
