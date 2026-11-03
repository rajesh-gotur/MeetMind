"""One bot run: join the call as a guest, record it, hear it, write the notes.

Phases (shown live in the popup):
  joining    fresh guest browser, "Ask to join" like a person
  waiting    someone in the call clicks Admit ("admitted" only when 2 signals agree)
  recording  captions on (bot's view only); 2-minute audio pieces are heard during the call;
             a watchdog checks sound, captions and the call every 5 s and repairs what it can
  finishing  the call ended (or Stop): 5 s more for the last captions, then the last piece
  writing    layers 5-12 (app/bot/finalize.py)
  done       the same files as Record here (zip, Word, Excel, MOM, README, transcript)

Everything is saved in backend/meetings/<id>/ so "Create notes again" works later.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.bot.browser import AudioPiece, BrowserMissingError, MeetBrowser
from app.bot.finalize import BotResult, MeetingData, NothingRecordedError, build_notes
from app.bot.links import parse_meet_link
from app.bot.state import BotStatus
from app.config import Settings
from app.pipeline import glossary_terms
from app.pipeline.ears import EarContext
from app.pipeline.store import MeetingFolder, MeetingStore
from app.pipeline.transcriber import Hearing, Transcriber
from app.services.notes import AnalysisError, EmptyTranscriptError
from app.services.usage import UsageLimiter

logger = logging.getLogger("meetmind.bot")

POLL_SECONDS = 0.5
WATCH_SECONDS = 5.0
SILENT_LEVEL = 0.02  # the call sound is "flat" below this level
FLAT_SECONDS = 12.0  # no sound for this long while captions show people talking -> repair the sound capture
MAX_SOUND_REPAIRS = 3
CAPTION_GRACE_SECONDS = 5.0  # after Stop: wait for the last captions (they lag about 2 s)
ADMIT_CHECKS = 2  # "in the call" must be seen this many times in a row
NO_AUDIO_WARNING_SECONDS = 45
BLOCKED_RETRIES = 3  # Meet sometimes refuses one try and lets a fresh browser in
NO_BUTTON_SECONDS = 45
BLOCKED_MESSAGE = (
    "Google Meet refused the bot's guest browser (\"You can't join this video call\"). "
    "Try again, and check the meeting lets guests ask to join (Host controls -> Meeting access)."
)
GONE_SECONDS = 8  # Meet redraws its toolbar now and then; only a lasting absence means the call ended
STOP_REASONS = {
    "button": "Stopped from MeetMind.",
    "you_left": "You left the meeting, so the bot stopped. Writing the notes now.",
}
CAPTION_RETRY_SECONDS = 5
CAPTION_TRIES = 6
YOU_SEEN_CHECKS = 4  # your name must be seen in the call this many times before your leaving counts
YOU_GONE_SECONDS = 15  # your name gone from the call this long = you left (a short redraw is not a leave)
SMALL_CALL = 6  # with more people Meet may not show every tile, so "your tile is gone" is not trusted
NAME_SUFFIX = re.compile(r"\s*\((you|presenting|presentation|host|co-host|meeting host)\)\s*$", re.IGNORECASE)


def same_person(shown: str, name: str) -> bool:
    """Meet's name for someone vs a full name: equal, or Meet shows only the first part.

    Real Meet showed the bot "MeetMind Notetaker" as "MeetMind", and may show
    "Rajesh Gotur" as "RAJESH GOTUR" or "Rajesh".
    """
    a = " ".join(NAME_SUFFIX.sub("", shown).split()).casefold()
    b = " ".join(NAME_SUFFIX.sub("", name).split()).casefold()
    if not a or not b:
        return False
    return a == b or b.startswith(a + " ") or a.startswith(b + " ")


@dataclass(frozen=True)
class BotRequest:
    meet_url: str
    language: str  # "auto", "en" or "hi"
    requested_by: str  # the person who sent the bot (shown in the notes)
    client_id: str


class BotSession:
    def __init__(self, settings: Settings, request: BotRequest, status: BotStatus, limiter: UsageLimiter) -> None:
        self.settings = settings
        self.config = settings.bot
        self.request = request
        self.status = status
        self.limiter = limiter
        self.browser = MeetBrowser(self.config)
        self.transcriber = Transcriber(settings)
        self.terms = glossary_terms(settings)
        self.store = MeetingStore(settings.storage_path(), settings.storage.keep_days, settings.storage.enabled)
        self.folder: MeetingFolder | None = None
        self.result: BotResult | None = None
        self._hearings: list[Hearing] = []
        self._jobs: list[asyncio.Task[None]] = []
        self._failed: list[tuple[int, float, bytes]] = []
        self._captions: dict[str, dict[str, Any]] = {}
        self._timeline: list[dict[str, Any]] = []
        self._open_talks: dict[str, dict[str, Any]] = {}
        self._roster: set[str] = set()
        self._gaps: list[str] = []
        self._title = ""
        self._started_wall: datetime | None = None
        self._clock = 0.0  # meeting seconds reported by the page

    def _is_bot(self, name: str) -> bool:
        return name.casefold() == "you" or same_person(name, self.config.bot_name)

    def _is_you(self, name: str) -> bool:
        return bool(self.request.requested_by) and same_person(name, self.request.requested_by)

    @property
    def meeting_code(self) -> str:
        link = parse_meet_link(self.request.meet_url)
        return link.code if link else "meeting"

    # ------------------------------------------------------------------ main flow

    async def run(self) -> None:
        self.status.update(
            phase="starting",
            meeting_url=self.request.meet_url,
            meeting_code=self.meeting_code,
            bot_name=self.config.bot_name,
            engine=self.transcriber.label,
        )
        self.store.cleanup()
        recorded = False
        try:
            await self.browser.start()
            self.status.update(phase="joining")
            self.status.event(f"Opening {self.request.meet_url}")
            await self.browser.open(self.request.meet_url)
            if await self._join():
                recorded = True
                await self._record()
        except BrowserMissingError as exc:
            self.status.fail(str(exc))
        except Exception as exc:  # noqa: BLE001 - report any browser failure instead of dying silently
            logger.exception("Bot failed while in the meeting")
            self.status.warn(f"The bot's browser stopped unexpectedly ({exc.__class__.__name__}).")
        finally:
            await self.browser.close()

        try:
            if self.status.get("phase") != "error" and recorded:
                await asyncio.gather(*self._jobs, return_exceptions=True)
                await self._retry_failed()
                self._save_meeting()
                await self._write_notes()
        finally:
            await self.transcriber.aclose()

    # ------------------------------------------------------------------ joining

    async def _join(self) -> bool:
        deadline = time.monotonic() + self.config.admit_timeout_minutes * 60
        asked = False
        last_screen = ""
        blocked_tries = 0
        in_call_seen = 0
        prejoin_since = time.monotonic()
        warned_no_button = False
        while time.monotonic() < deadline:
            if self.status.stop_requested.is_set():
                self.status.fail("Stopped before the bot was admitted.")
                return False
            snap = await self.browser.snapshot()
            screen = snap.get("screen") or "loading"
            if screen != last_screen:
                # Shown in the server window, so a failed join can be explained.
                logger.info("Meet screen: %s | %s", screen, (await self.browser.page_text())[:160].replace("\n", " "))
                last_screen = screen
            if screen == "incall":
                # Two signals must agree, twice in a row: the call toolbar AND people on screen.
                in_call_seen = in_call_seen + 1 if snap.get("present") else 0
                if in_call_seen >= ADMIT_CHECKS:
                    self.status.event("Admitted. The bot is in the call.")
                    return True
                await asyncio.sleep(1)
                continue
            in_call_seen = 0
            if screen == "blocked":
                blocked_tries += 1
                if blocked_tries <= BLOCKED_RETRIES:
                    await self.browser.save_screenshot(f"blocked-{blocked_tries}")
                    self.status.event(
                        f"Meet refused this try. Trying again in a fresh browser ({blocked_tries}/{BLOCKED_RETRIES})…"
                    )
                    self._waiting_reset()
                    await asyncio.sleep(random.uniform(4, 8))
                    try:
                        await self.browser.restart_fresh(self.request.meet_url)
                    except BrowserMissingError as exc:
                        self.status.fail(str(exc))
                        return False
                    asked = False
                    last_screen = ""
                    prejoin_since = time.monotonic()
                    continue
                return await self._give_up("blocked", BLOCKED_MESSAGE)
            if screen == "invalid":
                return await self._give_up("invalid", "This link does not open a Google Meet meeting. Check the link.")
            if screen == "denied" and asked:
                return await self._give_up("denied", "Someone in the call denied the bot's request to join.")
            if screen == "ended" and asked:
                return await self._give_up("ended", "The bot was not let into the call (the request ended).")
            if screen == "waiting":
                asked = True
                self._waiting()
            elif screen in {"prejoin", "loading"}:
                if await self.browser.ask_to_join(self.config.bot_name):
                    asked = True
                    self._waiting()
                elif not warned_no_button and time.monotonic() - prejoin_since > NO_BUTTON_SECONDS:
                    warned_no_button = True
                    shot = await self.browser.save_screenshot("no-join-button")
                    self.status.warn(
                        'The bot cannot find the "Ask to join" button yet. Look at the bot\'s window'
                        + (f" (picture saved: {shot})." if shot else ".")
                    )
            await asyncio.sleep(1)
        return await self._give_up(
            "timeout", f"Nobody clicked Admit within {self.config.admit_timeout_minutes:g} minutes."
        )

    async def _give_up(self, name: str, message: str) -> bool:
        shot = await self.browser.save_screenshot(name)
        logger.warning("Bot could not join: %s%s", message, f" Picture: {shot}" if shot else "")
        self.status.fail(message)
        return False

    def _waiting_reset(self) -> None:
        """Back to "joining" while a new try starts (the popup no longer says "Waiting for Admit")."""
        if self.status.get("phase") == "waiting":
            self.status.update(phase="joining")

    def _waiting(self) -> None:
        if self.status.get("phase") != "waiting":
            self.status.update(phase="waiting")
            self.status.event(f"Asked to join. Someone in the call needs to admit {self.config.bot_name}.")

    # ------------------------------------------------------------------ recording

    async def _record(self) -> None:
        cfg = self.config
        self._started_wall = datetime.now().astimezone()
        self.status.update(phase="recording", started_at=time.time())
        self.folder = self.store.create(self.meeting_code)
        if self.folder is not None:
            self.status.update(meeting_id=self.folder.id)
        problem = await self.browser.capture_call_audio()
        if problem:
            self.status.warn(f"Could not record the Meet tab's sound ({problem}); using Meet's audio elements instead.")
        elif cfg.silent:
            self.status.event("Recording the call's sound (silent: nothing is played on this computer's speakers).")
        else:
            self.status.event("Recording the call's sound.")
        offset, _ = await self.browser.next_piece()
        index, piece_offset, piece_started = 0, offset, time.monotonic()
        self.status.piece(0, state="recording")

        watch = _Watchdog()
        caption_tries, next_caption_try = 0, 0.0
        alone_since: float | None = None
        gone_since: float | None = None
        you_seen, you_gone_since = 0, None  # your own name in the call (stop_when_you_leave)
        warned_no_audio = False
        deadline = time.monotonic() + cfg.max_meeting_minutes * 60

        while True:
            snap = await self.browser.snapshot()
            self._absorb(snap)
            now = time.monotonic()
            watch.observe(snap, now)

            if not snap.get("captionsOn") and caption_tries < CAPTION_TRIES and now >= next_caption_try:
                caption_tries += 1
                next_caption_try = now + CAPTION_RETRY_SECONDS
                await self.browser.turn_on_captions()
                if caption_tries == CAPTION_TRIES:
                    self.status.warn("Captions could not be turned on in the bot's window; names come from voices.")
            if snap.get("captionsOn") and not self.status.get("captions_on"):
                self.status.update(captions_on=True)
                self.status.event("Captions on (only the bot sees them).")
            started = self.status.get("started_at") or time.time()
            if not warned_no_audio and not snap.get("tracks") and time.time() - started > NO_AUDIO_WARNING_SECONDS:
                warned_no_audio = True
                self.status.warn("No call audio has reached the bot yet.")

            if watch.due(now):
                await self._watch(watch, snap, now)

            # Alone = no other names on screen AND nobody heard (no sound, no new captions).
            # Both must hold, so a missed name tile alone never ends a meeting that is still going.
            others = [n for n in snap.get("present", []) if not self._is_bot(n)]
            alone = bool(self._roster) and not others and not watch.heard(snap)
            you_seen, you_gone_since, you_left = self._you_left(snap, others, you_seen, you_gone_since, now)
            alone_since = (alone_since or now) if alone else None
            gone_since = None if (not snap or snap.get("screen") == "incall") else (gone_since or now)

            reason = ""
            if snap.get("screen") in {"ended", "denied"} or (gone_since and now - gone_since > GONE_SECONDS):
                reason = "The meeting ended."
            elif self.status.stop_requested.is_set():
                reason = STOP_REASONS.get(self.status.stop_reason, STOP_REASONS["button"])
            elif you_left:
                self.status.stop_reason = "you_left"
                reason = STOP_REASONS["you_left"]
            elif alone_since and now - alone_since > cfg.leave_when_alone_minutes * 60:
                reason = "Everyone else left the call."
            elif now > deadline:
                reason = "Reached the maximum meeting length."
            if reason:
                self.status.event(reason)
                break

            if now - piece_started >= cfg.piece_seconds:
                offset, finished = await self.browser.next_piece()
                self._piece_done(index, piece_offset, finished)
                index, piece_offset, piece_started = index + 1, offset, now
                self.status.piece(index, state="recording")
            await asyncio.sleep(POLL_SECONDS)

        self.status.update(phase="finishing")
        if snap.get("screen") == "incall":
            # Still in the call (Stop or alone): the last words are still being captioned.
            self.status.event("Saving the last words…")
            grace_end = time.monotonic() + CAPTION_GRACE_SECONDS
            while time.monotonic() < grace_end:
                self._absorb(await self.browser.snapshot())
                await asyncio.sleep(POLL_SECONDS)
        self.status.update(ended_at=time.time())
        try:
            self._piece_done(index, piece_offset, await self.browser.last_piece())
            self._absorb(await self.browser.snapshot())
        except Exception as exc:  # noqa: BLE001 - the page may already be gone
            self.status.warn(f"The last audio piece could not be read ({exc.__class__.__name__}).")
        self._close_talks()
        await self.browser.leave()

    def _you_left(
        self, snap: dict[str, Any], others: list[str], seen: int, gone_since: float | None, now: float
    ) -> tuple[int, float | None, bool]:
        """Did YOU (the person who sent the bot) leave the call? Returns (seen, gone_since, left).

        Only trusted when your name was seen in the call several times, the call is small
        (Meet shows every tile) and nobody is presenting (a presentation hides tiles).
        """
        if not self.config.stop_when_you_leave or not self.request.requested_by or snap.get("screen") != "incall":
            return seen, None, False
        if any(self._is_you(n) for n in others):
            return seen + 1, None, False
        if seen < YOU_SEEN_CHECKS or snap.get("presenting") or len(others) >= SMALL_CALL:
            return seen, None, False
        gone_since = gone_since or now
        return seen, gone_since, now - gone_since >= YOU_GONE_SECONDS

    async def _watch(self, watch: _Watchdog, snap: dict[str, Any], now: float) -> None:
        """Every 5 s: is the bot in the call, is sound arriving, are captions on? Repair what it can."""
        in_call = snap.get("screen") == "incall"
        captions_on = bool(snap.get("captionsOn"))
        sound_ok = not watch.sound_lost(now)
        if not sound_ok and watch.repairs < MAX_SOUND_REPAIRS:
            watch.repaired(now)
            note = f"Captions show speech but no sound is arriving: restarting the sound capture ({watch.repairs})."
            self.status.warn(note)
            self._gaps.append("The call sound stopped for a while; the bot repaired it and captions filled the gap.")
            problem = await self.browser.restart_call_audio()
            if problem:
                self.status.warn(f"Sound capture restart failed ({problem}).")
        score = 100 - (0 if in_call else 40) - (0 if sound_ok else 35) - (0 if captions_on else 25)
        self.status.update(
            health={"score": max(score, 0), "in_call": in_call, "sound": sound_ok, "captions": captions_on}
        )

    def _absorb(self, snap: dict[str, Any]) -> None:
        """Takes what the page reported: title, people, new captions, who is talking, audio levels."""
        if snap.get("title"):
            self._title = re.sub(r"^Meet\s*[-–:]\s*", "", snap["title"]).strip()
        if isinstance(snap.get("t"), int | float):
            self._clock = float(snap["t"])
        self._roster.update(n for n in snap.get("roster", []) if not self._is_bot(n))
        speaking: set[str] = set()
        for entry in snap.get("captions", []):
            if self._is_bot(entry["speaker"]):
                continue
            old = self._captions.get(entry["id"])
            self._captions[entry["id"]] = entry
            speaking.add(entry["speaker"])
            self._roster.add(entry["speaker"])
            self.status.add_words(
                entry["speaker"], len(entry["text"].split()) - (len(old["text"].split()) if old else 0)
            )
        self._track_talking({n for n in snap.get("speaking", []) if not self._is_bot(n)})
        present = [n for n in snap.get("present", []) if not self._is_bot(n)]
        self.status.set_people(present, speaking | set(snap.get("speaking", [])))
        if snap.get("captions"):
            lines = sorted(self._captions.values(), key=lambda c: c["start"])
            self.status.set_captions([{"speaker": c["speaker"], "text": c["text"], "t": c["start"]} for c in lines])
        self.status.add_levels(snap.get("levels", []))
        self.status.update(tracks=snap.get("tracks", 0), title=self._title)

    def _track_talking(self, names: set[str]) -> None:
        """Who-is-talking timeline (layer 10 signal) from Meet's speaking highlight."""
        t = self._clock
        for name in names:
            talk = self._open_talks.get(name)
            if talk is not None and t - talk["end"] <= 1.5:
                talk["end"] = t
            else:
                self._open_talks[name] = {"name": name, "start": t, "end": t}
                self._timeline.append(self._open_talks[name])
        for name in list(self._open_talks):
            if name not in names and t - self._open_talks[name]["end"] > 1.5:
                del self._open_talks[name]

    def _close_talks(self) -> None:
        self._timeline = [t for t in self._timeline if t["end"] - t["start"] >= 0.4]
        self._open_talks.clear()

    # ------------------------------------------------------------------ hearing the pieces

    def _piece_done(self, index: int, offset: float, piece: AudioPiece | None) -> None:
        if piece is None:
            return
        if self.folder is not None:
            self.folder.save_piece(index, piece.audio)
        self._jobs.append(asyncio.create_task(self._hear(index, offset, piece.audio)))

    async def _hear(self, index: int, offset: float, audio: bytes, retry: bool = False) -> None:
        self.status.piece(index, state="uploading")
        names = sorted(self._roster | ({self.request.requested_by} if self.request.requested_by else set()))
        language = None if self.request.language == "auto" else self.request.language
        context = EarContext(terms=self.terms, names=names, language=language, track="tab", piece=index)
        try:
            hearing = await self.transcriber.hear(audio, "audio/webm", offset, context, self.terms)
        except Exception as exc:  # noqa: BLE001 - one piece never stops the meeting
            logger.exception("Hearing piece %s failed", index + 1)
            hearing = Hearing(piece=index, offset=offset, duration=0.0, level=0.0, failures=[str(exc)])
        heard = any(h.words for h in hearing.hypotheses)
        if not heard and not hearing.skipped and not retry:
            self._failed.append((index, offset, audio))
            self.status.piece(index, state="failed", error="; ".join(hearing.failures)[:300])
            self.status.warn(f"Audio piece {index + 1} could not be heard yet; it will be tried again.")
            return
        self._hearings.append(hearing)
        if self.folder is not None:
            self.folder.save_json(f"hearing-{index:03d}", hearing.to_dict())
        self.limiter.record(self.request.client_id, hearing.duration)
        if hearing.skipped:
            self.status.piece(index, state="done", note="silence")
            self.status.event(f"Audio piece {index + 1} was silent; nothing to transcribe.")
            return
        words = max((len(h.words) for h in hearing.hypotheses), default=0)
        self.status.piece(index, state="done", engine=" + ".join(hearing.ears))
        self.status.update(engine=" + ".join(hearing.ears) or self.status.get("engine"))
        self.status.event(f"Audio piece {index + 1} heard by {' + '.join(hearing.ears) or 'no ear'}: {words} words.")

    async def _retry_failed(self) -> None:
        for index, offset, audio in self._failed:
            self.status.event(f"Trying audio piece {index + 1} again.")
            await self._hear(index, offset, audio, retry=True)

    # ------------------------------------------------------------------ notes

    def _data(self) -> MeetingData:
        started = self.status.get("started_at") or time.time()
        wall = self._started_wall or datetime.now().astimezone()
        return MeetingData(
            code=self.meeting_code,
            title=self._title,
            bot_name=self.config.bot_name,
            requested_by=self.request.requested_by,
            started_at=started,
            ended_at=self.status.get("ended_at") or time.time(),
            started_wall=wall.isoformat(),
            roster=sorted(self._roster),
            captions=sorted(self._captions.values(), key=lambda c: c["start"]),
            timeline=self._timeline,
            gaps=list(dict.fromkeys(self._gaps)),
        )

    def _save_meeting(self) -> None:
        if self.folder is not None:
            self.folder.save_json("meeting", self._data().to_dict())

    async def _write_notes(self) -> None:
        self.status.update(phase="writing")
        data = self._data()
        try:
            self.result = await build_notes(self.settings, data, self._hearings, self.terms, self.status.event)
        except NothingRecordedError as exc:
            self.status.fail(str(exc))
            return
        except (AnalysisError, EmptyTranscriptError) as exc:
            self.status.fail(f"The notes could not be created: {exc}")
            return
        finish(self.status, self.result, self.folder)


async def renotes(settings: Settings, folder: MeetingFolder, status: BotStatus) -> BotResult | None:
    """ "Create notes again" from a saved meeting (no speech engine is called again)."""
    raw = folder.load_json("meeting")
    if not raw:
        status.fail("This meeting was not saved, so its notes cannot be made again.")
        return None
    data = MeetingData.from_dict(raw)
    hearings = [
        Hearing.from_dict(h)
        for h in (folder.load_json(path.stem) for path in sorted(folder.path.glob("hearing-*.json")))
        if h
    ]
    status.update(
        phase="writing",
        meeting_code=data.code,
        bot_name=data.bot_name,
        title=data.title,
        started_at=data.started_at,
        ended_at=data.ended_at,
        meeting_id=folder.id,
    )
    status.event("Creating the notes again from the saved meeting…")
    try:
        result = await build_notes(settings, data, hearings, glossary_terms(settings), status.event)
    except NothingRecordedError as exc:
        status.fail(str(exc))
        return None
    except (AnalysisError, EmptyTranscriptError) as exc:
        status.fail(f"The notes could not be created: {exc}")
        return None
    finish(status, result, folder)
    return result


def finish(status: BotStatus, result: BotResult, folder: MeetingFolder | None) -> None:
    notes = result.notes
    report = notes.report
    if folder is not None:
        folder.save_json("transcript", [t.model_dump() for t in notes.turns])
        folder.save_json("notes", notes.model_dump(mode="json"))
    status.set_captions([{"speaker": t.speaker, "text": t.text, "t": t.start} for t in notes.turns], final=True)
    status.update(
        phase="done",
        title=report.title or result.meeting.title,
        report={
            "title": report.title,
            "overview": report.overview,
            "action_items": [item.model_dump(mode="json") for item in report.action_items],
            "decisions": len(report.decisions),
            "corrections": report.corrections,
            "people": len(notes.participants),
            "duration": result.meeting.duration_seconds,
            "generated_by": report.generated_by,
            "quality": report.quality.model_dump() if report.quality else None,
        },
    )
    status.event("Done. Your meeting notes are ready to download.")


class _Watchdog:
    """Remembers when sound and captions were last seen, between the 5-second checks."""

    def __init__(self) -> None:
        self._started = time.monotonic()
        self._last_loud = self._started
        self._last_caption: float | None = None
        self._next = 0.0
        self.repairs = 0

    def observe(self, snap: dict[str, Any], now: float) -> None:
        if any(level >= SILENT_LEVEL for level in snap.get("levels") or []):
            self._last_loud = now
        if snap.get("captions"):  # captions only change while someone is talking
            self._last_caption = now

    @staticmethod
    def heard(snap: dict[str, Any]) -> bool:
        """Someone is talking right now: loud call sound or captions on screen."""
        return bool(snap.get("captions")) or any(level >= SILENT_LEVEL for level in snap.get("levels") or [])

    def due(self, now: float) -> bool:
        if now < self._next:
            return False
        self._next = now + WATCH_SECONDS
        return True

    def sound_lost(self, now: float) -> bool:
        """People are talking (captions keep changing) but no sound has arrived for FLAT_SECONDS."""
        talking = self._last_caption is not None and now - self._last_caption <= WATCH_SECONDS * 2
        return talking and now - self._last_loud >= FLAT_SECONDS

    def repaired(self, now: float) -> None:
        self.repairs += 1
        self._last_loud = now  # give the restarted capture time to deliver sound
