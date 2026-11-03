"""
Start the backend:        python run.py
Check config.yaml:        python run.py check
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from app.config import ConfigError, Settings

BAR = "=" * 64


def _load() -> Settings:
    try:
        return Settings.load()
    except ConfigError as exc:
        raise SystemExit(f"\nConfig problem: {exc}\n") from exc


def _mask(key: str) -> str:
    return f"{key[:4]}...{key[-4:]}" if len(key) > 12 else ("set" if key else "MISSING")


def describe(settings: Settings) -> None:
    from app.bot.browser import playwright_ready
    from app.pipeline import audio, glossary, glossary_terms, vad
    from app.pipeline.ears import IndicEar, LocalWhisperEar

    ears = settings.pipeline.ears
    on = {True: "on", False: "off"}
    print(f"  Folder          : {Path(__file__).resolve().parent}")
    print(f"  Listening on    : http://{settings.server.host}:{settings.server.port}")
    print(
        "  Keys            : "
        + ", ".join(
            f"{name} {_mask(key)}"
            for name, key in (
                ("Gemini", settings.gemini.api_key),
                ("Deepgram", settings.deepgram.api_key),
                ("Groq", settings.groq.api_key),
            )
        )
    )
    listening = [
        f"Gemini {settings.gemini.transcribe_model}" if ears.gemini and settings.gemini.api_key else "",
        f"Deepgram {settings.deepgram.model}" if ears.deepgram and settings.deepgram.api_key else "",
        "Meet captions (bot)" if ears.captions else "",
    ]
    print(f"  Ears            : {' + '.join(x for x in listening if x) or 'NONE'}")
    backups = [
        f"Groq {settings.groq.model}" if ears.groq_whisper and settings.groq.api_key else "",
        "local faster-whisper" if LocalWhisperEar(settings).ready else "",
        "AI4Bharat IndicConformer" if IndicEar(settings).ready else "",
    ]
    print(f"  Backup ears     : {', '.join(x for x in backups if x) or 'none'}")
    pipe = settings.pipeline
    print(
        f"  Layers          : audio prep {'PyAV' if audio.available() else 'OFF (pip install av)'}, "
        f"voice detection {vad.engine_name() if pipe.voice_detection else 'off'}, "
        f"glossary fixer {on[pipe.glossary_fixer and glossary.available()]}, AI fixer {on[pipe.ai_fixer]}"
    )
    terms = glossary_terms(settings)
    learned = len(terms) - len(settings.vocabulary.terms)
    print(f"  Glossary        : {len(settings.vocabulary.terms)} terms + {max(learned, 0)} learned")
    review = "reviewer AI on" if settings.notes.review else "reviewer off"
    print(f"  Notes           : facts first, {review}; models {', '.join(settings.gemini.models)}")
    ollama = settings.ollama
    print(f"  Local AI        : Ollama {ollama.model} ({ollama.enabled}; used only when it is running)")
    store = settings.storage
    kept = f"{settings.storage_path()} (kept {store.keep_days} days)" if store.enabled else "off"
    print(f"  Saved meetings  : {kept}")
    bot = settings.bot
    missing = playwright_ready()
    bot_state = "off (bot.enabled: false)" if not bot.enabled else "NOT INSTALLED" if missing else "ready"
    sound = "silent (no echo)" if bot.silent else "plays the call sound"
    print(f'  MeetMind bot    : {bot_state}, joins as a guest "{bot.bot_name}", {sound}')
    token = settings.server.app_token
    source = "from APP_TOKEN environment variable" if os.environ.get("APP_TOKEN") else "from config.yaml"
    shown = f"{token[:4]}...{token[-4:]} ({source}; must match appToken in extension/config.js)"
    print(f"  App token       : {shown if token else 'MISSING'}")
    print(f"  Per-user limit  : {settings.limits.daily_audio_minutes_per_user} audio minutes / day")
    ids = settings.cors.allowed_extension_ids
    print(f"  Extensions      : {', '.join(ids) if ids else 'any (protected by app token)'}")
    for problem in settings.problems():
        print(f"  PROBLEM         : {problem}")
    for note in settings.warnings():
        print(f"  NOTE            : {note}")


def _port_busy(port: int) -> bool:
    """True when something already answers on this port on this computer."""
    import socket

    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else "start"
    if command not in {"start", "check"}:
        raise SystemExit(__doc__)

    settings = _load()
    print(f"\n{BAR}\n  MeetMind backend v{settings.app_version}\n{BAR}")
    describe(settings)

    if command == "check":
        if settings.problems():
            raise SystemExit(f"{BAR}\n  Fix the problems above, then run this again.\n")
        print(f"{BAR}\n  config.yaml is valid.\n")
        return

    if _port_busy(settings.server.port):
        raise SystemExit(
            f"{BAR}\n  Port {settings.server.port} is already used by ANOTHER server (maybe an old MeetMind).\n"
            "  Windows lets two servers share a port, and the old one keeps answering.\n"
            f"  Stop it first:  netstat -ano | findstr :{settings.server.port}   then   taskkill /F /PID <the number>\n"
        )
    print(f"{BAR}\n")
    import uvicorn

    uvicorn.run(
        "app.main:create_app",
        factory=True,
        host=settings.server.host,
        port=settings.server.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
