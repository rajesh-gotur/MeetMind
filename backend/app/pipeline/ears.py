"""Layers 3-6 — the "ears": independent speech engines that each transcribe the same audio.

  Ear 1  Gemini 3.5 Transcribe   best words; glossary given as custom vocabulary
  Ear 2  Deepgram Nova-3         exact word timings, word confidence, voice groups
  Ear 3  Meet captions           (bot only) built from the captions the bot already read
  Backup Groq Whisper            used when Gemini or Deepgram fails
  Extra  local faster-whisper / AI4Bharat IndicConformer (optional, offline)
         run only on doubtful parts, see transcriber.py

Every ear returns a Hypothesis; it never raises for an engine problem (it returns
an EarFailure instead), so one broken engine never stops the meeting.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import httpx
import numpy as np

from app.config import Settings
from app.pipeline.audio import SAMPLE_RATE, PreparedAudio
from app.pipeline.hypothesis import HWord, Hypothesis
from app.services.stt_common import SpeechRequest, TranscriptionError, post_with_retry, unique_terms
from app.services.stt_deepgram import query_params
from app.services.stt_groq import GroqTranscriber

logger = logging.getLogger(__name__)

GEMINI_VOCABULARY_LIMIT = 100  # Google: "best results are typically achieved with up to 100 terms"
TRANSCRIBE_PROMPT = (
    "Transcribe this meeting audio exactly as spoken (verbatim), in the language spoken. "
    "Do not translate, summarise or add speaker labels. Spell these words exactly like this: {terms}. "
    "Return only the transcript text."
)


@dataclass(frozen=True)
class EarContext:
    terms: list[str]  # glossary + learned terms
    names: list[str]  # participant names
    language: str | None  # "en", "hi" or None (automatic)
    track: str = "tab"  # "tab", "mic" or "mix"
    piece: int = 0  # piece number (voice groups are only meaningful inside one piece)
    need_timings: bool = False  # True when no other ear gives word times


@dataclass
class EarResults:
    hypotheses: list[Hypothesis] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)  # plain-language notes for the quality report

    def get(self, source: str) -> Hypothesis | None:
        return next((h for h in self.hypotheses if h.source == source), None)


class EarError(RuntimeError):
    pass


# --------------------------------------------------------------------------- Deepgram


class DeepgramEar:
    source = "deepgram"

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client

    @property
    def ready(self) -> bool:
        return bool(self._settings.deepgram.api_key) and self._settings.pipeline.ears.deepgram

    @property
    def label(self) -> str:
        return f"Deepgram {self._settings.deepgram.model}"

    async def hear(self, audio: PreparedAudio, ctx: EarContext) -> Hypothesis:
        deepgram = self._settings.deepgram
        request = SpeechRequest(
            audio=audio.data,
            filename=audio.filename,
            content_type=audio.mime_type,
            offset_seconds=audio.offset_seconds,
            track=ctx.track,  # type: ignore[arg-type]
            language=ctx.language,
            names=ctx.names,
            terms=ctx.terms,
        )
        headers = {"Authorization": f"Token {deepgram.api_key}", "Content-Type": audio.mime_type}
        try:
            payload = await post_with_retry(
                "Deepgram",
                deepgram.max_retries,
                lambda: self._client.post(
                    f"{deepgram.base_url}/listen",
                    params=query_params(self._settings, request),
                    headers=headers,
                    content=audio.data,
                    timeout=deepgram.timeout_seconds,
                ),
            )
        except TranscriptionError as exc:
            raise EarError(str(exc)) from exc
        return Hypothesis(self.source, self.label, parse_deepgram(payload, audio.offset_seconds, ctx.piece))


def parse_deepgram(payload: dict[str, Any], offset: float, piece: int) -> list[HWord]:
    results = payload.get("results") or {}
    channel = (results.get("channels") or [{}])[0]
    raw_words = ((channel.get("alternatives") or [{}])[0]).get("words") or []
    if not raw_words:  # some answers carry the words only inside utterances
        raw_words = [w for u in results.get("utterances") or [] for w in u.get("words") or []]
    words: list[HWord] = []
    for raw in raw_words:
        text = str(raw.get("punctuated_word") or raw.get("word") or "").strip()
        if not text:
            continue
        speaker = raw.get("speaker")
        words.append(
            HWord(
                text=text,
                start=round(float(raw.get("start", 0.0)) + offset, 3),
                end=round(float(raw.get("end", 0.0)) + offset, 3),
                confidence=float(raw.get("confidence", 0.9) or 0.9),
                speaker=f"dg:{piece}:{speaker}" if speaker is not None else None,
            )
        )
    return words


# --------------------------------------------------------------------------- Gemini


class GeminiEar:
    """Gemini 3.5 Transcribe; when that model is not available, Gemini audio understanding."""

    source = "gemini"

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client

    @property
    def ready(self) -> bool:
        return bool(self._settings.gemini.api_key) and self._settings.pipeline.ears.gemini

    @property
    def label(self) -> str:
        return f"Gemini {self._settings.gemini.transcribe_model or self._settings.gemini.models[0]}"

    async def hear(self, audio: PreparedAudio, ctx: EarContext) -> Hypothesis:
        gemini = self._settings.gemini
        if gemini.transcribe_model:
            try:
                return await self._transcribe_model(audio, ctx)
            except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
                logger.warning("Gemini Transcribe failed (%s); using Gemini audio understanding", _short(exc))
        try:
            return await self._audio_prompt(audio, ctx)
        except (httpx.HTTPError, KeyError, ValueError, TypeError, IndexError) as exc:
            raise EarError(f"Gemini: {_short(exc)}") from exc

    async def _transcribe_model(self, audio: PreparedAudio, ctx: EarContext) -> Hypothesis:
        gemini = self._settings.gemini
        config: dict[str, Any] = {"mode": "verbatim"}
        if ctx.need_timings:
            # Word times cannot be combined with custom vocabulary in one request.
            config["timestamp_granularities"] = ["word"]
        else:
            config["custom_vocabulary"] = unique_terms(ctx.names, ctx.terms, limit=GEMINI_VOCABULARY_LIMIT)
        languages = _language_codes(ctx.language, gemini.transcribe_languages)
        if languages:
            config["language_codes"] = languages
        body = {
            "model": gemini.transcribe_model,
            "input": [{"type": "audio", "data": base64.b64encode(audio.data).decode(), "mime_type": audio.mime_type}],
            "generation_config": {"transcription_config": config},
        }
        response = await self._client.post(
            f"{gemini.base_url}/interactions",
            headers={"x-goog-api-key": gemini.api_key},
            json=body,
            timeout=gemini.transcribe_timeout_seconds,
        )
        response.raise_for_status()
        text, timed = parse_interaction(response.json(), audio.offset_seconds)
        if timed:
            return Hypothesis(self.source, f"Gemini {gemini.transcribe_model}", timed, timed=True)
        return Hypothesis(self.source, f"Gemini {gemini.transcribe_model}", _untimed(text, audio), timed=False)

    async def _audio_prompt(self, audio: PreparedAudio, ctx: EarContext) -> Hypothesis:
        gemini = self._settings.gemini
        model = gemini.models[0]
        terms = ", ".join(unique_terms(ctx.names, ctx.terms, limit=GEMINI_VOCABULARY_LIMIT)) or "none"
        body = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"inline_data": {"mime_type": audio.mime_type, "data": base64.b64encode(audio.data).decode()}},
                        {"text": TRANSCRIBE_PROMPT.format(terms=terms)},
                    ],
                }
            ],
            "generationConfig": {"temperature": 0.0},
        }
        response = await self._client.post(
            f"{gemini.base_url}/models/{model}:generateContent",
            headers={"x-goog-api-key": gemini.api_key},
            json=body,
            timeout=gemini.transcribe_timeout_seconds,
        )
        response.raise_for_status()
        parts = response.json()["candidates"][0]["content"]["parts"]
        text = " ".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
        return Hypothesis(self.source, f"Gemini {model} (audio)", _untimed(text, audio), timed=False)


def parse_interaction(payload: dict[str, Any], offset: float) -> tuple[str, list[HWord]]:
    """Text and (when requested) timed words from an Interactions API answer."""
    texts: list[str] = []
    timed: list[HWord] = []
    for step in payload.get("steps") or []:
        if step.get("type") not in (None, "model_output"):
            continue
        for content in step.get("content") or []:
            if content.get("type") not in (None, "text"):
                continue
            texts.append(str(content.get("text") or ""))
            for note in content.get("annotations") or []:
                if note.get("type") != "word_info" or not str(note.get("text") or "").strip():
                    continue
                start, end = _seconds(note.get("start_offset")), _seconds(note.get("end_offset"))
                if start is None or end is None:
                    continue
                timed.append(HWord(str(note["text"]).strip(), round(start + offset, 3), round(end + offset, 3), 0.9))
    text = str(payload.get("output_text") or "").strip() or " ".join(t.strip() for t in texts if t.strip())
    if not text and not timed:
        raise ValueError("Gemini returned no transcript")
    return text, timed


def _seconds(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    match = re.match(r"^\s*([\d.]+)\s*s?\s*$", str(value))
    return float(match.group(1)) if match else None


def _language_codes(language: str | None, auto: list[str]) -> list[str]:
    if language == "en":
        return ["en-IN"]
    if language == "hi":
        return ["hi-IN"]
    return list(auto)


def _untimed(text: str, audio: PreparedAudio) -> list[HWord]:
    """Text without times: spread the words over the piece (fusion aligns them by text)."""
    tokens = [t for t in re.sub(r"\s+", " ", text).split(" ") if t]
    if not tokens:
        return []
    duration = max(audio.duration_seconds, 1.0)
    step = duration / len(tokens)
    return [
        HWord(t, round(audio.offset_seconds + i * step, 3), round(audio.offset_seconds + (i + 1) * step, 3), 0.85)
        for i, t in enumerate(tokens)
    ]


# --------------------------------------------------------------------------- Groq (backup)


class GroqEar:
    source = "groq"

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._engine = GroqTranscriber(settings, client)

    @property
    def ready(self) -> bool:
        return bool(self._settings.groq.api_key) and self._settings.pipeline.ears.groq_whisper

    @property
    def label(self) -> str:
        return self._engine.label

    async def hear(self, audio: PreparedAudio, ctx: EarContext) -> Hypothesis:
        try:
            result = await self._engine.transcribe(
                SpeechRequest(
                    audio=audio.data,
                    filename=audio.filename,
                    content_type=audio.mime_type,
                    offset_seconds=audio.offset_seconds,
                    track=ctx.track,  # type: ignore[arg-type]
                    language=ctx.language,
                    names=ctx.names,
                    terms=ctx.terms,
                )
            )
        except TranscriptionError as exc:
            raise EarError(str(exc)) from exc
        words = [HWord(w.text, w.start, w.end, 0.75) for seg in result.segments for w in seg.words]
        if not words:  # no word times: spread the segment text
            for seg in result.segments:
                tokens = seg.text.split()
                step = max(seg.end - seg.start, 0.1) / max(len(tokens), 1)
                words += [HWord(t, seg.start + i * step, seg.start + (i + 1) * step, 0.7) for i, t in enumerate(tokens)]
        return Hypothesis(self.source, self.label, words, timed=True, language=result.language)


# --------------------------------------------------------------------------- local models (optional)


def _enabled(switch: str, available: bool) -> bool:
    return switch == "on" or (switch == "auto" and available)


def _installed(module: str) -> bool:
    try:
        __import__(module)
    except ImportError:
        return False
    return True


@lru_cache(maxsize=1)
def _whisper_model(size: str, compute: str) -> Any:
    from faster_whisper import WhisperModel

    logger.info("Loading local faster-whisper '%s' (first use only)", size)
    return WhisperModel(size, device="cpu", compute_type=compute)


@lru_cache(maxsize=1)
def _indic_model(name: str) -> Any:
    from transformers import AutoModel

    logger.info("Loading AI4Bharat IndicConformer (first use only)")
    return AutoModel.from_pretrained(name, trust_remote_code=True)


class LocalWhisperEar:
    """faster-whisper on the CPU: English-heavy doubtful spans, and the offline backup."""

    source = "local_whisper"
    label = "local faster-whisper"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def ready(self) -> bool:
        return _enabled(self._settings.pipeline.ears.local_whisper, _installed("faster_whisper"))

    async def hear_span(self, samples: np.ndarray, start: float, ctx: EarContext) -> Hypothesis:
        models = self._settings.local_models

        def run() -> list[HWord]:
            model = _whisper_model(models.whisper_model, models.whisper_compute)
            segments, _ = model.transcribe(
                samples,
                language=ctx.language if ctx.language in {"en", "hi"} else None,
                word_timestamps=True,
                initial_prompt=", ".join(unique_terms(ctx.names, ctx.terms, limit=40)) or None,
                vad_filter=False,
                condition_on_previous_text=False,
            )
            return [
                HWord(w.word.strip(), round(start + w.start, 3), round(start + w.end, 3), float(w.probability))
                for seg in segments
                for w in seg.words or []
                if w.word.strip()
            ]

        return Hypothesis(self.source, self.label, await asyncio.to_thread(run), timed=True)


class IndicEar:
    """AI4Bharat IndicConformer: Hindi-heavy doubtful spans (best open Hindi model in public benchmarks)."""

    source = "indic"
    label = "AI4Bharat IndicConformer"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def ready(self) -> bool:
        return _enabled(self._settings.pipeline.ears.indic, _installed("transformers") and _installed("torch"))

    async def hear_span(self, samples: np.ndarray, start: float, ctx: EarContext) -> Hypothesis:
        name = self._settings.local_models.indic_model

        def run() -> str:
            import torch

            model = _indic_model(name)
            wav = torch.from_numpy(samples.astype(np.float32)).unsqueeze(0)  # [1, samples] at 16 kHz
            peak = torch.max(torch.abs(wav))
            if peak > 0:
                wav = wav / peak
            with torch.no_grad():
                result = model(wav, "hi", "ctc")
            if isinstance(result, list):
                result = result[0] if result else ""
            return str(result).strip()

        text = await asyncio.to_thread(run)
        span = PreparedAudio(b"", "audio/wav", start, samples.size / SAMPLE_RATE)
        return Hypothesis(self.source, self.label, _untimed(text, span), timed=False, language="hi")


def _short(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return f"{exc.__class__.__name__}: {str(exc)[:160]}"
