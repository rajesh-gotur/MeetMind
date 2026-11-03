"""Everything that touches the Google Meet page.

Google Meet refuses browsers started by test tools (Playwright's own launch adds
flags such as --enable-automation and --no-sandbox, and Meet answers
"You can't join this video call"). So the bot starts your real Google Chrome as
a normal program, with its own profile folder, and Playwright only connects to it
through Chrome's debugging port. To Meet it is an ordinary Chrome window.

The page itself is instrumented by page_hooks.js (call audio, captions, people,
screen state); this class drives it: open, ask to join, turn captions on, cut
audio pieces, leave. Playwright is imported lazily so the server still starts
(with the bot switched off) when it is not installed.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import BotConfig

BACKEND_DIR = Path(__file__).resolve().parents[2]
HOOKS = (Path(__file__).resolve().parent / "page_hooks.js").read_text(encoding="utf-8")
WORLD = "meetmind"  # name of the hidden (isolated) world the bot's page code runs in
logger = logging.getLogger("meetmind.bot")
# Only ordinary Chrome switches: nothing that marks the browser as automated.
CHROME_ARGS = [
    "--no-first-run",
    "--no-default-browser-check",
    "--autoplay-policy=no-user-gesture-required",  # call audio plays without a click
    "--lang=en-US",  # Meet's button labels are read in English
    "--auto-accept-this-tab-capture",  # lets the bot record the Meet tab's sound once it is in the call
    "--window-size=1280,860",
]
CONNECT_SECONDS = 30
READ_SECONDS = 3.0  # time a person takes to look at the join page before typing
# Pop-up answers that keep the microphone and camera off. "Use microphone" is never clicked.
SAFE_BUTTONS = re.compile(
    r"^(continue without (microphone and camera|microphone|camera)|not now|no thanks|got it|dismiss|ok)$",
    re.IGNORECASE,
)


class BrowserMissingError(RuntimeError):
    """Playwright or Chrome is not installed, or the bot's Chrome could not start."""


@dataclass(frozen=True)
class AudioPiece:
    audio: bytes
    peak: float


def _async_driver() -> Any:
    """patchright (Playwright without its tell-tale debugger traces) when installed, else Playwright."""
    try:
        from patchright.async_api import async_playwright
    except ImportError:
        from playwright.async_api import async_playwright
    return async_playwright


def playwright_ready() -> str:
    """Empty when the bot can run, else a short reason."""
    try:
        import importlib

        importlib.import_module("patchright")
    except ImportError:
        try:
            import playwright  # noqa: F401
        except ImportError:
            return (
                "The bot is not installed on the server: run  pip install -r requirements.txt"
                "  then  python -m playwright install chromium"
            )
    return ""


def profile_path(config: BotConfig) -> Path:
    """The bot's guest browser profile (a fresh one is made for every try)."""
    path = Path(config.profile_dir)
    base = path if path.is_absolute() else BACKEND_DIR / path
    return base / "guest"


def is_headless(config: BotConfig) -> bool:
    """bot.headless: true (or the older bot.show_browser: false) runs Chrome without a window."""
    return config.headless or not config.show_browser


def find_chrome(config: BotConfig) -> str:
    """Your installed Google Chrome; else the Chromium that Playwright installed."""
    candidates: list[str] = []
    if config.browser == "chrome":
        if sys.platform == "win32":
            for base in (
                os.environ.get("PROGRAMFILES"),
                os.environ.get("PROGRAMFILES(X86)"),
                os.environ.get("LOCALAPPDATA"),
            ):
                if base:
                    candidates.append(str(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"))
        elif sys.platform == "darwin":
            candidates.append("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        else:
            candidates += [shutil.which(n) or "" for n in ("google-chrome", "google-chrome-stable")]
            candidates.append("/opt/google/chrome/chrome")
    for path in candidates:
        if path and Path(path).is_file():
            return path
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            path = playwright.chromium.executable_path
        if Path(path).is_file():
            return path
    except Exception:  # noqa: BLE001 - reported below
        pass
    raise BrowserMissingError(
        "Google Chrome was not found. Install Google Chrome, or run  python -m playwright install chromium"
    )


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


BLOCK = 2  # Chrome's "Block" value for a site permission


def prepare_profile(folder: Path) -> None:
    """Default settings for the bot's Chrome, written before it starts.

    Microphone and camera stay on Chrome's normal "ask" (exactly like a new incognito window;
    the bot never clicks Allow, it answers Meet's question "without"). Notifications are
    blocked, and Chrome is marked as closed cleanly so it does not show "Restore pages?".
    """
    prefs_file = folder / "Default" / "Preferences"
    try:
        prefs = json.loads(prefs_file.read_text(encoding="utf-8")) if prefs_file.is_file() else {}
    except (OSError, ValueError):
        prefs = {}
    profile = prefs.setdefault("profile", {})
    defaults = profile.setdefault("default_content_setting_values", {})
    for name in ("media_stream_mic", "media_stream_camera"):
        defaults.pop(name, None)  # back to "ask"
    defaults["notifications"] = BLOCK
    profile.update({"exit_type": "Normal", "exited_cleanly": True})
    prefs.setdefault("browser", {})["check_default_browser"] = False
    try:
        prefs_file.parent.mkdir(parents=True, exist_ok=True)
        prefs_file.write_text(json.dumps(prefs), encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not write the bot's Chrome settings: %s", exc)


def reset_guest_profile(config: BotConfig) -> None:
    """The bot starts every try with a brand-new browser profile, like a new incognito window."""
    folder = profile_path(config)
    shutil.rmtree(folder, ignore_errors=True)
    if folder.exists():
        logger.warning("Could not clear the bot's guest profile %s (is a bot window still open?)", folder)


def start_chrome(config: BotConfig, url: str, debug_port: int | None = None) -> subprocess.Popen[bytes]:
    """Starts Chrome as a normal program with the bot's own profile folder."""
    folder = profile_path(config)
    folder.mkdir(parents=True, exist_ok=True)
    prepare_profile(folder)
    args = [find_chrome(config), f"--user-data-dir={folder}", *CHROME_ARGS]
    if debug_port:
        args += [f"--remote-debugging-port={debug_port}", "--remote-allow-origins=*"]
    if is_headless(config):
        args.append("--headless=new")
    if sys.platform.startswith("linux") and os.geteuid() == 0:
        args.append("--no-sandbox")  # Chrome will not start as root without it (servers, containers)
    args.append(url)
    logger.info("Bot browser: %s, joins as a guest, profile %s", args[0], folder)
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # noqa: S603


class MeetBrowser:
    def __init__(self, config: BotConfig) -> None:
        self._config = config
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._process: subprocess.Popen[bytes] | None = None
        self._cdp: Any = None
        self._name_seen_at: float | None = None
        self.page: Any = None

    async def start(self) -> None:
        if reason := playwright_ready():
            raise BrowserMissingError(reason)
        reset_guest_profile(self._config)
        port = _free_port()
        self._process = start_chrome(self._config, "about:blank", debug_port=port)
        self._playwright = await _async_driver()().start()
        deadline = time.monotonic() + CONNECT_SECONDS
        while True:
            if self._process.poll() is not None:
                await self.close()
                raise BrowserMissingError(
                    "The bot's Chrome closed at once. Is a bot window (or bot-login) already open? Close it, try again."
                )
            try:
                self._browser = await self._playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                break
            except Exception as exc:  # noqa: BLE001 - Chrome is still starting
                if time.monotonic() > deadline:
                    await self.close()
                    raise BrowserMissingError(
                        f"Could not connect to the bot's Chrome ({exc.__class__.__name__})."
                    ) from exc
                await asyncio.sleep(0.5)
        self._context = self._browser.contexts[0]
        self._name_seen_at = None

    async def restart_fresh(self, url: str) -> None:
        """Closes the bot's Chrome and opens the meeting again in a brand-new one (new guest profile)."""
        await self.close()
        await asyncio.sleep(1.5)  # Chrome releases its profile folder
        await self.start()
        await self.open(url)

    async def open(self, url: str) -> None:
        pages = self._context.pages
        self.page = pages[0] if pages else await self._context.new_page()
        if is_headless(self._config):
            await self._hide_headless_name()
        await self.page.goto(url, wait_until="domcontentloaded", timeout=60_000)

    async def _hide_headless_name(self) -> None:
        """Headless Chrome calls itself "HeadlessChrome", which Meet refuses; use the normal name."""
        try:
            version = await (await self._browser.new_browser_cdp_session()).send("Browser.getVersion")
            agent = str(version.get("userAgent", "")).replace("HeadlessChrome", "Chrome")
            session = await self._context.new_cdp_session(self.page)
            await session.send("Network.setUserAgentOverride", {"userAgent": agent, "acceptLanguage": "en-US"})
        except Exception as exc:  # noqa: BLE001 - Meet may then refuse; the log says why
            logger.warning("Could not set the headless browser's name: %s", exc)

    async def run_hidden(self, function: str, arg: Any = None, user_gesture: bool = False) -> Any:
        """Runs a JS function in the bot's hidden world on the Meet page and returns its result.

        The world is invisible to Meet's scripts (Google's bot check sees an unchanged page).
        page_hooks.js is loaded into it once per page load.
        """
        if self._cdp is None:
            self._cdp = await self._context.new_cdp_session(self.page)
        cdp = self._cdp
        frame = (await cdp.send("Page.getFrameTree"))["frameTree"]["frame"]["id"]
        world = await cdp.send("Page.createIsolatedWorld", {"frameId": frame, "worldName": WORLD})
        context = world["executionContextId"]

        async def evaluate(expression: str) -> Any:
            reply = await cdp.send(
                "Runtime.evaluate",
                {
                    "expression": expression,
                    "contextId": context,
                    "awaitPromise": True,
                    "returnByValue": True,
                    "userGesture": user_gesture,
                },
            )
            if "exceptionDetails" in reply:
                details = reply["exceptionDetails"]
                raise RuntimeError(details.get("exception", {}).get("description") or details.get("text"))
            return reply.get("result", {}).get("value")

        if not await evaluate("Boolean(window.__meetmind)"):
            await evaluate(HOOKS)
        return await evaluate(f"({function})({json.dumps(arg)})")

    async def page_text(self) -> str:
        try:
            return str(await self.run_hidden("() => document.body ? document.body.innerText : ''"))
        except Exception:  # noqa: BLE001 - the page is navigating
            return ""

    async def save_screenshot(self, name: str) -> Path | None:
        """Keeps a picture of what Meet showed, for troubleshooting (backend/bot-debug)."""
        folder = BACKEND_DIR / "bot-debug"
        try:
            folder.mkdir(exist_ok=True)
            path = folder / f"{time.strftime('%Y%m%d-%H%M%S')}-{name}.png"
            await self.page.screenshot(path=str(path))
            return path
        except Exception:  # noqa: BLE001 - only a debugging aid
            return None

    async def close(self) -> None:
        if self._browser is not None:
            try:
                cdp = await self._browser.new_browser_cdp_session()
                await cdp.send("Browser.close")
            except Exception:  # noqa: BLE001 - already closed
                pass
        for closer in (self._browser, self._playwright):
            try:
                if closer is not None:
                    await (closer.close() if closer is self._browser else closer.stop())
            except Exception:  # noqa: BLE001 - already closed
                pass
        if self._process is not None:
            try:
                await asyncio.to_thread(self._process.wait, 5)
            except subprocess.TimeoutExpired:
                self._process.kill()
        self._browser = self._context = self._playwright = self._process = self._cdp = None

    # ---- reading the page

    async def snapshot(self) -> dict[str, Any]:
        """Screen state, people, new captions and audio levels since the last call."""
        sel = self._config.selectors
        options = {
            "tileSelector": sel.tile,
            "nameSelector": sel.name,
            "peopleListSelector": sel.people_list,
            "captionRegionSelector": sel.caption_region,
            "leaveButtonSelector": sel.leave_button,
            "speakingSelector": sel.speaking,
        }
        try:
            data = await self.run_hidden("(o) => window.__meetmind.snapshot(o)", options)
        except Exception:  # noqa: BLE001 - the page is navigating
            return {}
        return data or {}

    # ---- actions

    async def dismiss_popups(self) -> None:
        """Closes Meet's pop-ups (device questions, tips) without ever turning a microphone or camera on."""
        page = self.page
        for label in ("Got it", "Dismiss", "Not now", "No thanks"):
            if await _click(page.get_by_role("button", name=label, exact=True)):
                logger.info("Bot closed a Meet tip (%s)", label)
        for _ in range(3):
            dialog = page.locator('[role="dialog"]:visible, [role="alertdialog"]:visible').last
            try:
                if not await dialog.is_visible(timeout=300):
                    return
            except Exception:  # noqa: BLE001 - the page is navigating
                return
            if await _click(dialog.get_by_role("button", name=SAFE_BUTTONS)):
                logger.info("Bot answered a Meet pop-up without microphone or camera")
            elif await _click(dialog.locator('button[aria-label*="close" i], [role="button"][aria-label*="close" i]')):
                logger.info("Bot closed a Meet pop-up")
            else:
                await page.keyboard.press("Escape")
                logger.info("Bot closed a Meet pop-up with Escape")
            await asyncio.sleep(0.4)

    async def ask_to_join(self, bot_name: str) -> bool:
        """Closes pop-ups, types the bot's name and clicks "Ask to join", at a person's pace.

        Google's "not a bot" check watches how the page is used: the bot waits a moment like a
        person reading the page, moves the mouse, presses the keys one by one, then clicks.
        True when a join button was clicked.
        """
        sel = self._config.selectors
        page = self.page
        await self.dismiss_popups()
        name_box = page.locator(sel.name_input).first
        try:
            name_visible = await name_box.is_visible(timeout=500)
            needs_name = name_visible and not await name_box.input_value()
        except Exception:  # noqa: BLE001 - no name box when the browser is signed in
            name_visible = needs_name = False
        if name_visible and self._name_seen_at is None:
            self._name_seen_at = time.monotonic()
            await _wander(page)
            return False
        if self._name_seen_at is not None and time.monotonic() - self._name_seen_at < READ_SECONDS:
            return False  # still "reading" the page
        if needs_name:
            if not await _click(name_box):
                return False
            await asyncio.sleep(random.uniform(0.3, 0.7))
            await page.keyboard.type(bot_name, delay=random.uniform(70, 140))
            logger.info("Bot typed its name: %s", bot_name)
            await asyncio.sleep(random.uniform(0.8, 1.6))
            await self.dismiss_popups()  # typing can open a new pop-up
        pattern = re.compile("|".join(re.escape(b) for b in sel.join_buttons), re.IGNORECASE)
        clicked = await _click(page.get_by_role("button", name=pattern))
        if clicked:
            logger.info("Bot clicked the join button")
        return clicked

    async def turn_on_captions(self) -> None:
        """Captions are shown only in the bot's window; nobody else in the call sees them."""
        if not await _click(self.page.locator(self._config.selectors.captions_button)):
            await self.page.keyboard.press("c")  # Meet's keyboard shortcut

    async def leave(self) -> None:
        await _click(self.page.locator(self._config.selectors.leave_button))

    # ---- audio

    async def capture_call_audio(self) -> str:
        """Starts recording the Meet tab's sound. Empty string when it works, else the reason.

        With bot.silent the sound is recorded but not played on this computer's speakers.
        """
        try:
            result = await self.run_hidden(
                "(silent) => window.__meetmind.captureTab(silent)", self._config.silent, user_gesture=True
            )
        except Exception as exc:  # noqa: BLE001 - reported to the person
            return f"{exc.__class__.__name__}: {exc}"
        return "" if result and result.get("ok") else str((result or {}).get("error") or "unknown reason")

    async def restart_call_audio(self) -> str:
        """Watchdog repair: stop and start the tab-sound capture again. Empty string when it works."""
        try:
            await self.run_hidden("() => window.__meetmind.stopTab()")
        except Exception:  # noqa: BLE001 - nothing to stop
            pass
        return await self.capture_call_audio()

    async def next_piece(self) -> tuple[float, AudioPiece | None]:
        """Starts a new audio piece; returns its start (seconds) and the finished previous piece."""
        result = await self.run_hidden("() => window.__meetmind.rotate()")
        return float(result["offset"]), _piece(result.get("finished"))

    async def last_piece(self) -> AudioPiece | None:
        return _piece(await self.run_hidden("() => window.__meetmind.stopAll()"))


def _piece(raw: dict[str, Any] | None) -> AudioPiece | None:
    if not raw:
        return None
    return AudioPiece(audio=base64.b64decode(raw["b64"]), peak=float(raw.get("peak", 0.0)))


async def _click(locator: Any) -> bool:
    """Clicks the first visible match like a person: the mouse travels there, then clicks.

    Does nothing when the element is not on screen.
    """
    try:
        target = locator.first
        if not await target.is_visible(timeout=300):
            return False
        box = await target.bounding_box()
        if not box:
            await target.click(timeout=2000)
            return True
        page = target.page
        x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
        y = box["y"] + box["height"] * random.uniform(0.3, 0.7)
        await page.mouse.move(x + random.uniform(-60, 60), y + random.uniform(-40, 40), steps=random.randint(8, 15))
        await page.mouse.move(x, y, steps=random.randint(5, 10))
        await asyncio.sleep(random.uniform(0.08, 0.25))
        await page.mouse.down()
        await asyncio.sleep(random.uniform(0.05, 0.12))
        await page.mouse.up()
        return True
    except Exception:  # noqa: BLE001 - the element is optional
        return False


async def _wander(page: Any) -> None:
    """A few natural mouse movements while the page is "read"."""
    try:
        size = page.viewport_size or {"width": 1200, "height": 800}
        for _ in range(random.randint(2, 4)):
            await page.mouse.move(
                random.uniform(100, size["width"] - 100),
                random.uniform(100, size["height"] - 100),
                steps=random.randint(10, 20),
            )
            await asyncio.sleep(random.uniform(0.2, 0.6))
    except Exception:  # noqa: BLE001 - only for realism
        pass
