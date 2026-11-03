"""Runs one bot at a time on its own thread and event loop.

The bot needs a browser subprocess. On Windows that requires a Proactor event loop,
which the web server's loop may not be, so the bot always gets a fresh loop of the
right kind on its own thread. The web server only reads its status.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
from collections.abc import Coroutine
from dataclasses import dataclass, replace
from typing import Any

from app.bot.browser import playwright_ready
from app.bot.finalize import BotResult
from app.bot.links import parse_meet_link
from app.bot.session import BotRequest, BotSession, renotes
from app.bot.state import BotStatus
from app.config import Settings
from app.pipeline.store import MeetingStore
from app.services.usage import UsageLimiter

logger = logging.getLogger("meetmind.bot")


class BotUnavailableError(RuntimeError):
    """The bot cannot start (switched off, not installed, or already busy)."""


@dataclass
class _Run:
    status: BotStatus
    thread: threading.Thread
    session: BotSession | None = None  # None for "Create notes again"
    result: BotResult | None = None


class BotManager:
    def __init__(self, settings: Settings, limiter: UsageLimiter) -> None:
        self._settings = settings
        self._limiter = limiter
        self._lock = threading.Lock()
        self._run: _Run | None = None
        self._idle = BotStatus()
        self._store = MeetingStore(settings.storage_path(), settings.storage.keep_days, settings.storage.enabled)

    @property
    def status(self) -> BotStatus:
        return self._run.status if self._run else self._idle

    @property
    def result(self) -> BotResult | None:
        if self._run is None:
            return None
        return self._run.session.result if self._run.session else self._run.result

    def unavailable_reason(self) -> str:
        if not self._settings.bot.enabled:
            return "The bot is switched off on this server (bot.enabled in config.yaml)."
        return playwright_ready()

    def start(self, request: BotRequest) -> BotStatus:
        link = parse_meet_link(request.meet_url)
        if link is None:
            raise ValueError("This is not a Google Meet link. Copy the link from the meeting (meet.google.com/...).")
        request = replace(request, meet_url=link.url)
        if reason := self.unavailable_reason():
            raise BotUnavailableError(reason)
        with self._lock:
            self._ensure_idle()
            status = BotStatus()
            session = BotSession(self._settings, request, status, self._limiter)
            thread = threading.Thread(target=_run_in_own_loop, args=(session.run(), status), name="meetmind-bot")
            thread.daemon = True
            self._run = _Run(status=status, thread=thread, session=session)
            thread.start()
        logger.info("Bot started for %s", request.meet_url)
        return status

    def renotes(self, meeting_id: str = "") -> BotStatus:
        """ "Create notes again" for the last (or the given) saved meeting, without recording again."""
        meeting_id = meeting_id or (self.status.get("meeting_id") if self._run else "")
        folder = self._store.open(meeting_id) if meeting_id else None
        if folder is None:
            raise BotUnavailableError("No saved meeting to create notes from (storage.enabled must be true).")
        with self._lock:
            self._ensure_idle()
            status = BotStatus()
            run = _Run(status=status, thread=threading.Thread(name="meetmind-notes"))

            async def job() -> None:
                run.result = await renotes(self._settings, folder, status)

            run.thread = threading.Thread(target=_run_in_own_loop, args=(job(), status), name="meetmind-notes")
            run.thread.daemon = True
            self._run = run
            run.thread.start()
        return status

    def _ensure_idle(self) -> None:
        if self._run and self._run.status.active:
            raise BotUnavailableError("The bot is already working. Stop it first.")

    def stop(self, reason: str = "button") -> None:
        """reason: "button" (Stop in the popup) or "you_left" (you left the same meeting)."""
        if self._run and self._run.status.active:
            if self._run.status.stop_requested.is_set():
                return  # already stopping
            self._run.status.stop_reason = reason
            self._run.status.stop_requested.set()
            left = reason == "you_left"
            self._run.status.event("You left the meeting, so the bot is stopping." if left else "Stop requested.")

    def reset(self) -> None:
        with self._lock:
            if self._run and self._run.status.active:
                raise BotUnavailableError("The bot is still working. Stop it first.")
            self._run = None


def _run_in_own_loop(job: Coroutine[Any, Any, None], status: BotStatus) -> None:
    if sys.platform == "win32":
        loop: asyncio.AbstractEventLoop = asyncio.ProactorEventLoop()  # browser subprocesses need Proactor
    else:
        loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(job)
    except Exception:  # noqa: BLE001 - never let the thread die without a status
        logger.exception("Bot crashed")
        status.fail("The bot stopped because of an unexpected error. See the server window.")
    finally:
        loop.close()
