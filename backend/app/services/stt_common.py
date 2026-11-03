"""Pieces shared by every speech-to-text engine."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

import httpx

from app.schemas import TranscriptSegment

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}

# "mic": only the recorder's own voice. "tab": everyone else in the call. "mix": both (older extensions).
Track = Literal["mic", "tab", "mix"]


class TranscriptionError(RuntimeError):
    """An engine could not transcribe a piece after all retries."""

    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class SpeechRequest:
    audio: bytes
    filename: str
    content_type: str
    offset_seconds: float
    track: Track = "mix"
    language: str | None = None
    # Participant names seen in the call; spelled exactly like this.
    names: list[str] = field(default_factory=list)
    # Project words from config.yaml (vocabulary.terms).
    terms: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ChunkTranscript:
    duration_seconds: float
    language: str | None
    segments: list[TranscriptSegment]
    engine: str = ""


def retry_delay(response: httpx.Response | None, attempt: int) -> float:
    """Honour the server's retry-after header, else back off exponentially."""
    if response is not None:
        retry_after = response.headers.get("retry-after")
        if retry_after:
            try:
                return min(float(retry_after), 60.0)
            except ValueError:
                pass
    return float(2**attempt)


async def post_with_retry(
    name: str,
    max_retries: int,
    send: Callable[[], Awaitable[httpx.Response]],
) -> dict:
    """Calls ``send`` until it answers 200, retrying busy or network errors only."""
    last_error = "unknown error"
    for attempt in range(max_retries + 1):
        response: httpx.Response | None = None
        try:
            response = await send()
        except httpx.HTTPError as exc:
            last_error = f"network error: {exc.__class__.__name__}"
        else:
            if response.status_code == 200:
                return response.json()
            last_error = f"{name} returned {response.status_code}"
            if response.status_code not in RETRYABLE_STATUS:
                raise TranscriptionError(f"{last_error}: {response.text[:300]}", status_code=502)
        if attempt < max_retries:
            delay = retry_delay(response, attempt)
            logger.warning("%s attempt %s failed (%s); retrying in %.1fs", name, attempt + 1, last_error, delay)
            await asyncio.sleep(delay)
    status = 429 if "429" in last_error else 502
    raise TranscriptionError(f"{name} failed after retries: {last_error}", status_code=status)


def unique_terms(*groups: list[str], limit: int = 100) -> list[str]:
    """Merges names and terms, keeping the first spelling of each and dropping blanks."""
    seen: dict[str, str] = {}
    for group in groups:
        for raw in group:
            term = " ".join(str(raw).split())[:60]
            if term and term.lower() not in seen:
                seen[term.lower()] = term
    return list(seen.values())[:limit]
