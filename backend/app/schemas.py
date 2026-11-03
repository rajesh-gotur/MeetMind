"""Pydantic models shared by the API and services (single source of truth)."""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str


class AuthCheckResponse(BaseModel):
    ok: bool = True
    speech_to_text_ready: bool
    speech_engine: str = ""
    summary_ready: bool = False
    minutes_left_today: float


class Word(BaseModel):
    """One spoken word with its own time, so speakers can change mid-sentence."""

    text: str
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    # Voice number from the speech engine (Deepgram), only meaningful inside one audio piece.
    speaker: int | None = None
    # 0-1: how sure MeetMind is about this word after comparing every engine ("ear").
    confidence: float | None = None
    # Other spellings the ears heard here (only for doubtful words).
    alternatives: list[str] = Field(default_factory=list)


class TranscriptSegment(BaseModel):
    """One spoken phrase, with times relative to the start of the meeting."""

    start: float = Field(ge=0, description="Seconds from meeting start")
    end: float = Field(ge=0, description="Seconds from meeting start")
    text: str
    # Voice number from the speech engine (Deepgram diarization), else None.
    speaker: int | None = None
    words: list[Word] = Field(default_factory=list)


class TranscribeResponse(BaseModel):
    chunk_index: int
    offset_seconds: float
    duration_seconds: float
    language: str | None = None
    engine: str = ""
    segments: list[TranscriptSegment]


class ErrorResponse(BaseModel):
    detail: str


# ---------------------------------------------------------------------------
# Meeting report (Phase 2)
# ---------------------------------------------------------------------------

Priority = Literal["High", "Medium", "Low"]


class Turn(BaseModel):
    """One speaker turn: Whisper words labelled with the caption speaker."""

    start: float = Field(ge=0)
    end: float = Field(ge=0)
    speaker: str = Field(min_length=1, max_length=120)
    text: str = Field(max_length=20_000)


class MeetingInfo(BaseModel):
    title: str = Field(max_length=200)
    code: str = Field(default="", max_length=60)
    started_at: datetime
    duration_seconds: float = Field(ge=0)
    recorded_by: str = Field(default="", max_length=120)


class AnalyzeRequest(BaseModel):
    meeting: MeetingInfo
    participants: list[str] = Field(default_factory=list, max_length=200)
    turns: list[Turn] = Field(max_length=20_000)
    # Placeholder speaker labels (for example "Speaker 2") the server may replace with a real participant name.
    unresolved_speakers: list[str] = Field(default_factory=list, max_length=200)


class Topic(BaseModel):
    title: str
    summary: str
    start: float
    end: float
    speakers: list[str] = Field(default_factory=list)


class Decision(BaseModel):
    text: str
    made_by: str = ""
    time: float | None = None


class ActionItem(BaseModel):
    id: str
    task: str
    owner: str
    assigned_by: str = ""
    due_date: date | None = None
    due_text: str = ""
    priority: Priority = "Medium"
    time: float = 0
    evidence: str = ""
    verified: bool = True


class Blocker(BaseModel):
    text: str
    raised_by: str = ""
    time: float | None = None


class QualityScore(BaseModel):
    """How much MeetMind trusts this meeting's notes (0-100, estimated)."""

    overall: int = Field(default=0, ge=0, le=100)
    capture: int = Field(default=0, ge=0, le=100)  # share of the meeting with usable sound or captions
    words: int = Field(default=0, ge=0, le=100)  # share of words confirmed by 2+ ears (or high confidence)
    speakers: int = Field(default=0, ge=0, le=100)  # share of words whose speaker is confirmed
    notes: int = Field(default=0, ge=0, le=100)  # share of tasks/decisions backed by a verified quote
    ears: list[str] = Field(default_factory=list)  # engines that contributed
    fallbacks: list[str] = Field(default_factory=list)  # layers that were skipped or replaced, in plain words
    doubtful_words: int = 0
    corrected_words: int = 0


class MeetingReport(BaseModel):
    title: str
    overview: str
    key_points: list[str] = Field(default_factory=list)
    topics: list[Topic] = Field(default_factory=list)
    decisions: list[Decision] = Field(default_factory=list)
    action_items: list[ActionItem] = Field(default_factory=list)
    blockers: list[Blocker] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    next_meeting: str = ""
    generated_by: str = ""
    corrections: int = 0
    notes: list[str] = Field(default_factory=list)
    quality: QualityScore | None = None


class AnalyzeResponse(BaseModel):
    report: MeetingReport
    # The transcript after spelling fixes and speaker naming; use it for every file.
    turns: list[Turn]
    participants: list[str] = Field(default_factory=list)


class ScreenshotInfo(BaseModel):
    """Metadata for one uploaded screenshot (the image travels as a file part with the same name)."""

    name: str = Field(pattern=r"^shot-\d{1,4}\.(jpg|jpeg|png)$")
    time: float = Field(ge=0)


class ExportRequest(BaseModel):
    meeting: MeetingInfo
    participants: list[str] = Field(default_factory=list)
    turns: list[Turn]
    report: MeetingReport
    screenshots: list[ScreenshotInfo] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------


class BotStopRequest(BaseModel):
    # "button" = Stop in the popup; "you_left" = the extension saw you leave the same meeting.
    reason: Literal["button", "you_left"] = "button"


class BotStartRequest(BaseModel):
    meet_url: str = Field(min_length=10, max_length=300)
    language: Literal["auto", "en", "hi"] = "auto"
    # The person who sent the bot, as their name appears in Meet (helps spell it right).
    your_name: str = Field(default="", max_length=120)
