"""Backend settings.

Everything lives in one file, ``config.yaml``. Secrets may instead come from
environment variables (used on Render), which always win over the file:

    DEEPGRAM_API_KEY -> deepgram.api_key
    GROQ_API_KEY   -> groq.api_key
    GEMINI_API_KEY -> gemini.api_key
    APP_TOKEN      -> server.app_token
    PORT           -> server.port
    BOT_ENABLED    -> bot.enabled (set "false" on hosts without a screen, such as Render)
    MEETMIND_CONFIG = path to a different config file (optional)
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError

BACKEND_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = BACKEND_DIR / "config.yaml"
PLACEHOLDER_TOKENS = {"", "change-me", "change-me-to-a-long-random-string"}


class ConfigError(RuntimeError):
    """config.yaml is missing or invalid."""


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = Field(default=8000, ge=1, le=65535)
    app_token: str = ""


class SpeechConfig(BaseModel):
    # Kept so older config files still load; the ears in "pipeline" decide which engines listen.
    engine: Literal["auto", "deepgram", "groq"] = "auto"


class DeepgramConfig(BaseModel):
    api_key: str = ""
    model: str = "nova-3"
    # Language used when the extension says "Auto": "multi" handles English mixed with Hindi.
    auto_language: str = "multi"
    base_url: str = "https://api.deepgram.com/v1"
    timeout_seconds: float = Field(default=180, gt=0)
    max_retries: int = Field(default=2, ge=0, le=10)


class VocabularyConfig(BaseModel):
    # Words every engine and correction layer must spell exactly like this.
    terms: list[str] = Field(default_factory=list, max_length=100)
    # Fix misheard words and name the speakers with Gemini before writing the notes.
    correct_transcript: bool = True
    # Remember terms that were corrected in past meetings and give them to the engines next time.
    learn: bool = True


class GroqConfig(BaseModel):
    api_key: str = ""
    model: str = "whisper-large-v3"
    base_url: str = "https://api.groq.com/openai/v1"
    timeout_seconds: float = Field(default=120, gt=0)
    max_retries: int = Field(default=3, ge=0, le=10)
    summary_fallback_model: str = "openai/gpt-oss-120b"


class GeminiConfig(BaseModel):
    api_key: str = ""
    models: list[str] = Field(default_factory=lambda: ["gemini-3.5-flash-lite", "gemini-3.8-flash"], min_length=1)
    base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    # Longest wait for one model before the next one is tried.
    timeout_seconds: float = Field(default=45, gt=0)
    # "low" keeps answers fast; "high" thinks longer; "" sends no thinking setting.
    thinking_level: str = "low"
    # Speech-to-text "ear" (Gemini 3.5 Transcribe). Empty = skip it.
    transcribe_model: str = "gemini-3.5-transcribe"
    transcribe_timeout_seconds: float = Field(default=180, gt=0)
    # Languages expected when the extension says "Auto" (English and Hindi, Indian accents).
    transcribe_languages: list[str] = Field(default_factory=lambda: ["en-IN", "hi-IN"])


Switch = Literal["on", "off", "auto"]


class EarsConfig(BaseModel):
    """Independent speech-to-text engines ("ears"). Their answers are compared word by word."""

    gemini: bool = True  # Gemini 3.5 Transcribe with your glossary (best words, Hinglish)
    deepgram: bool = True  # Deepgram Nova-3 (exact word timings and confidence)
    captions: bool = True  # Meet captions seen by the bot (bot mode only)
    groq_whisper: bool = True  # free backup when another ear fails
    # Heavy local models (pip install -r requirements-extra.txt). "auto" = use when installed.
    local_whisper: Switch = "auto"
    indic: Switch = "auto"


class PipelineConfig(BaseModel):
    """The accuracy layers every recording passes through after it is saved."""

    ears: EarsConfig = Field(default_factory=EarsConfig)
    voice_detection: bool = True  # Silero VAD: cut at pauses, skip silence
    glossary_fixer: bool = True  # spelling and sound-alike fixes against the glossary and names
    ai_fixer: bool = True  # an AI chooses between the ears' versions of doubtful words
    max_doubts_per_call: int = Field(default=80, ge=5, le=300)
    # Words scored below this are "doubtful" (0-1).
    doubt_below: float = Field(default=0.72, ge=0, le=1)


class LocalModelsConfig(BaseModel):
    """Optional offline models (requirements-extra.txt). They run only on doubtful parts."""

    whisper_model: str = "small"  # faster-whisper size: tiny, base, small, medium, large-v3
    whisper_compute: str = "int8"  # int8 runs well on a laptop CPU
    indic_model: str = "ai4bharat/indic-conformer-600m-multilingual"


class OllamaConfig(BaseModel):
    """Optional local AI (https://ollama.com) used when the online AIs are unavailable."""

    enabled: Switch = "auto"
    base_url: str = "http://localhost:11434"
    model: str = "llama3.1:8b"
    timeout_seconds: float = Field(default=120, gt=0)


class NotesConfig(BaseModel):
    # Facts first: extract and verify facts, write the notes, then a second AI reviews them.
    review: bool = True
    # Long meetings are read in sections of about this many minutes.
    section_minutes: float = Field(default=12, ge=3, le=60)


class StorageConfig(BaseModel):
    """Each meeting is saved so "Create notes again" works and nothing is lost on a crash."""

    enabled: bool = True
    folder: str = "meetings"  # inside backend, or a full path such as "C:/MeetMind-meetings"
    keep_days: int = Field(default=7, ge=1, le=365)


class QualityConfig(BaseModel):
    show_in_files: bool = True  # "Quality" section in MOM.md and the Word report


class BotSelectors(BaseModel):
    """Where things are on the Google Meet page. Only change these if Google redesigns Meet."""

    name_input: str = 'input[aria-label*="name" i], input[placeholder*="name" i]'
    join_buttons: list[str] = Field(default_factory=lambda: ["Ask to join", "Join now", "Join anyway"])
    mic_off_button: str = '[aria-label*="turn off microphone" i]'
    camera_off_button: str = '[aria-label*="turn off camera" i]'
    captions_button: str = 'button[aria-label*="turn on captions" i]'
    leave_button: str = 'button[aria-label*="leave call" i]'
    tile: str = "[data-participant-id]"
    name: str = ".notranslate"
    # Who-is-talking signal on a tile. Best effort: Meet does not document it.
    speaking: str = (
        '[data-audio-level]:not([data-audio-level="0"]), [class*="speaking" i], [aria-label*="is speaking" i]'
    )
    people_list: str = '[role="list"] [role="listitem"][aria-label]'
    caption_region: str = '[role="region"][aria-label*="caption" i]'


class BotConfig(BaseModel):
    """The MeetMind bot: joins a Meet call as a guest and records it, on this server's computer."""

    enabled: bool = True
    bot_name: str = Field(default="MeetMind Notetaker", min_length=2, max_length=60)
    admit_timeout_minutes: float = Field(default=10, gt=0)
    # Everyone else left (no names, no captions, no sound): the bot stops and writes the notes after this.
    leave_when_alone_minutes: float = Field(default=0.5, gt=0)
    max_meeting_minutes: float = Field(default=180, gt=0)
    # Audio is cut in pieces of this length (with a 2 s overlap) and checked during the call.
    piece_seconds: int = Field(default=120, ge=10)
    # Meet often refuses hidden browsers, so the bot's window is shown (it can be minimised).
    show_browser: bool = True
    # true = the bot's Chrome runs with no window at all (headless). Meet blocks headless browsers more often.
    headless: bool = False
    # true = the bot records the call sound but does NOT play it on this computer's speakers,
    # so you hear no echo when you are in the same meeting on the same PC.
    silent: bool = True
    # true = the bot stops and writes the notes when YOU (the person who sent it) leave the call.
    # The bot sees your name disappear from the call, so it works even if you joined from a phone.
    stop_when_you_leave: bool = True
    # "chrome" = your installed Google Chrome (Meet trusts it more); "chromium" = the browser Playwright installs.
    browser: Literal["chromium", "chrome"] = "chrome"
    # The bot's own browser data (a fresh guest profile is made here for every try).
    profile_dir: str = "bot-profile"
    selectors: BotSelectors = Field(default_factory=BotSelectors)


class LimitsConfig(BaseModel):
    daily_audio_minutes_per_user: int = Field(default=300, ge=1)
    max_chunk_mb: int = Field(default=25, ge=1, le=100)
    max_screenshots: int = Field(default=60, ge=0, le=300)


class CorsConfig(BaseModel):
    # Empty list = any Chrome extension may call the API (the app token still protects it).
    allowed_extension_ids: list[str] = Field(default_factory=list)


class Settings(BaseModel):
    app_name: str = "MeetMind API"
    app_version: str = "5.0.0"
    server: ServerConfig = Field(default_factory=ServerConfig)
    speech: SpeechConfig = Field(default_factory=SpeechConfig)
    deepgram: DeepgramConfig = Field(default_factory=DeepgramConfig)
    groq: GroqConfig = Field(default_factory=GroqConfig)
    vocabulary: VocabularyConfig = Field(default_factory=VocabularyConfig)
    gemini: GeminiConfig = Field(default_factory=GeminiConfig)
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)
    local_models: LocalModelsConfig = Field(default_factory=LocalModelsConfig)
    ollama: OllamaConfig = Field(default_factory=OllamaConfig)
    notes: NotesConfig = Field(default_factory=NotesConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    quality: QualityConfig = Field(default_factory=QualityConfig)
    bot: BotConfig = Field(default_factory=BotConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    cors: CorsConfig = Field(default_factory=CorsConfig)

    @property
    def daily_audio_cap_seconds(self) -> int:
        return self.limits.daily_audio_minutes_per_user * 60

    @property
    def max_upload_bytes(self) -> int:
        return self.limits.max_chunk_mb * 1024 * 1024

    def storage_path(self) -> Path:
        path = Path(self.storage.folder)
        return path if path.is_absolute() else BACKEND_DIR / path

    def problems(self) -> list[str]:
        """Setup mistakes that stop transcription from working."""
        issues: list[str] = []
        if not self.groq.api_key and not self.deepgram.api_key and not self.gemini.api_key:
            issues.append(
                "No speech-to-text key: set gemini.api_key, deepgram.api_key or groq.api_key "
                "(or the GEMINI_API_KEY / DEEPGRAM_API_KEY / GROQ_API_KEY environment variables)."
            )
        if self.server.app_token in PLACEHOLDER_TOKENS:
            issues.append("server.app_token is not set; pick a long random string (or set APP_TOKEN).")
        return issues

    def warnings(self) -> list[str]:
        """Settings that work but lower accuracy."""
        notes: list[str] = []
        if not self.gemini.api_key:
            notes.append("gemini.api_key is empty: no Gemini ear and the notes use the Groq backup model.")
        if not self.deepgram.api_key:
            notes.append("deepgram.api_key is empty: word timings come from Gemini/Groq (slightly less exact).")
        if not self.groq.api_key:
            notes.append("groq.api_key is empty: no free backup ear and no second AI to review the notes.")
        return notes

    @classmethod
    def load(cls, path: Path | None = None) -> Settings:
        config_path = path or Path(os.environ.get("MEETMIND_CONFIG", DEFAULT_CONFIG_PATH))
        data: dict[str, Any] = {}
        if config_path.exists():
            try:
                data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError as exc:
                raise ConfigError(f"{config_path.name} is not valid YAML: {exc}") from exc
            if not isinstance(data, dict):
                raise ConfigError(f"{config_path.name} must contain sections like 'server:' and 'groq:'.")
        elif path is not None:
            raise ConfigError(f"Config file not found: {config_path}")

        _apply_env(data)
        try:
            return cls.model_validate(data)
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(p) for p in first["loc"])
            raise ConfigError(f"config.yaml -> {where}: {first['msg']}") from exc


def _apply_env(data: dict[str, Any]) -> None:
    overrides = {
        ("deepgram", "api_key"): os.environ.get("DEEPGRAM_API_KEY"),
        ("groq", "api_key"): os.environ.get("GROQ_API_KEY"),
        ("gemini", "api_key"): os.environ.get("GEMINI_API_KEY"),
        ("server", "app_token"): os.environ.get("APP_TOKEN"),
        ("server", "port"): os.environ.get("PORT"),
        ("bot", "enabled"): os.environ.get("BOT_ENABLED"),
    }
    for (section, key), value in overrides.items():
        if value:
            data.setdefault(section, {})
            if not isinstance(data[section], dict):
                raise ConfigError(f"config.yaml -> '{section}' must be a section, not a single value.")
            data[section][key] = value


@lru_cache
def get_settings() -> Settings:
    return Settings.load()
