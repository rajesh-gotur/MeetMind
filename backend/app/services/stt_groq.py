"""Speech-to-text with Groq's OpenAI-compatible Whisper endpoint (free tier)."""

from __future__ import annotations

import httpx

from app.config import Settings
from app.schemas import TranscriptSegment, Word
from app.services.stt_common import ChunkTranscript, SpeechRequest, TranscriptionError, post_with_retry, unique_terms

# Whisper reads at most about 224 tokens of prompt; longer prompts are cut from the start.
PROMPT_CHARS = 700


def build_prompt(names: list[str], terms: list[str]) -> str:
    """
    Whisper treats the prompt as the text that came *before* the audio, so it copies
    its spelling and style. A plain list of words works; an instruction such as
    "Participants: ..." does not (Whisper then writes "PARTICIPANT 1" into the transcript).
    """
    words = unique_terms(names, terms)
    if not words:
        return ""
    prompt = ", ".join(words) + "."
    return prompt[-PROMPT_CHARS:]


def parse_segments(payload: dict, offset_seconds: float) -> list[TranscriptSegment]:
    """Converts Groq verbose_json (segments + words) to meeting-relative segments with words."""
    words = [
        Word(
            text=str(raw.get("word", "")).strip(),
            start=round(float(raw.get("start", 0.0)) + offset_seconds, 2),
            end=round(float(raw.get("end", 0.0)) + offset_seconds, 2),
        )
        for raw in payload.get("words") or []
        if str(raw.get("word", "")).strip()
    ]
    segments: list[TranscriptSegment] = []
    for raw in payload.get("segments") or []:
        text = str(raw.get("text", "")).strip()
        if not text or _is_hallucination(raw):
            continue
        start = round(float(raw.get("start", 0.0)) + offset_seconds, 2)
        end = round(max(float(raw.get("end", 0.0)) + offset_seconds, start), 2)
        inside = [w for w in words if start - 0.05 <= (w.start + w.end) / 2 <= end + 0.05]
        segments.append(TranscriptSegment(start=start, end=end, text=text, words=inside))
    return segments


def _is_hallucination(raw: dict) -> bool:
    """Whisper invents text over silence; its own scores reveal it."""
    no_speech = float(raw.get("no_speech_prob", 0.0) or 0.0)
    avg_logprob = float(raw.get("avg_logprob", 0.0) or 0.0)
    return no_speech > 0.6 and avg_logprob < -1.0


class GroqTranscriber:
    name = "groq"

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client

    @property
    def label(self) -> str:
        return f"Groq {self._settings.groq.model}"

    async def transcribe(self, request: SpeechRequest) -> ChunkTranscript:
        groq = self._settings.groq
        if not groq.api_key:
            raise TranscriptionError("Server has no Groq API key.", status_code=500)

        data: dict[str, str | list[str]] = {
            "model": groq.model,
            "response_format": "verbose_json",
            "temperature": "0",
            # Word times let MeetMind split a phrase where the speaker changes.
            "timestamp_granularities[]": ["word", "segment"],
        }
        if request.language:
            data["language"] = request.language
        prompt = build_prompt(request.names, request.terms)
        if prompt:
            data["prompt"] = prompt

        url = f"{groq.base_url}/audio/transcriptions"
        headers = {"Authorization": f"Bearer {groq.api_key}"}
        client = self._client or httpx.AsyncClient(timeout=groq.timeout_seconds)
        try:
            payload = await post_with_retry(
                "Groq",
                groq.max_retries,
                lambda: client.post(
                    url,
                    headers=headers,
                    data=data,
                    files={"file": (request.filename, request.audio, request.content_type)},
                ),
            )
        finally:
            if self._client is None:
                await client.aclose()

        return ChunkTranscript(
            duration_seconds=float(payload.get("duration") or 0.0),
            language=payload.get("language"),
            segments=parse_segments(payload, request.offset_seconds),
            engine=self.label,
        )
