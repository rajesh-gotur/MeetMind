"""Runs layers 1-8 for one audio piece ("hearing"), then fuses pieces into a transcript.

  hear()   during the meeting, once per piece:
           L1 prepare -> L2 voice detection -> L3/L4 Gemini + Deepgram in parallel
           (Groq when one fails, local Whisper when all online ears fail)
           -> quick vote -> L6 specialist local ears on doubtful spans
  fuse()   when the captions are complete (bot) or right away (Record here):
           L5 captions -> L7 vote -> L8 glossary fixer

A hearing is plain data (JSON-friendly), so the bot can save it and
"Create notes again" never has to call the speech engines a second time.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx
import numpy as np

from app.config import Settings
from app.pipeline import audio as audio_prep
from app.pipeline import vad
from app.pipeline.captions import caption_hypothesis
from app.pipeline.ears import DeepgramEar, EarContext, EarError, GeminiEar, GroqEar, IndicEar, LocalWhisperEar
from app.pipeline.fusion import FusedWord, fuse, respread
from app.pipeline.glossary import GlossaryFixer
from app.pipeline.hypothesis import HWord, Hypothesis
from app.pipeline.vad import Region

logger = logging.getLogger(__name__)

SILENT_LEVEL = 0.02  # no speech found AND quieter than this = a silent piece (skipped)
SPECIALIST_MAX_SECONDS = 60.0  # local models never spend longer than this per piece
SPAN_PAD_SECONDS = 0.6
_DEVANAGARI = range(0x0900, 0x0980)


@dataclass
class Hearing:
    """Everything the ears heard for one piece."""

    piece: int
    offset: float
    duration: float
    level: float
    speech: list[tuple[float, float]] = field(default_factory=list)  # meeting seconds
    hypotheses: list[Hypothesis] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    prepared: bool = False
    skipped: bool = False  # silent piece: no engine was called

    @property
    def end(self) -> float:
        return self.offset + self.duration

    @property
    def ears(self) -> list[str]:
        return [h.label for h in self.hypotheses if h.words]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Hearing:
        hyps = [
            Hypothesis(
                source=h["source"],
                label=h["label"],
                words=[HWord(**w) for w in h["words"]],
                timed=h.get("timed", True),
                language=h.get("language"),
            )
            for h in data.get("hypotheses", [])
        ]
        return cls(
            piece=data["piece"],
            offset=data["offset"],
            duration=data["duration"],
            level=data.get("level", 0.0),
            speech=[tuple(s) for s in data.get("speech", [])],  # type: ignore[misc]
            hypotheses=hyps,
            failures=data.get("failures", []),
            prepared=data.get("prepared", False),
            skipped=data.get("skipped", False),
        )


class Transcriber:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(180, connect=15))
        self._owns_client = client is None
        self.gemini = GeminiEar(settings, self._client)
        self.deepgram = DeepgramEar(settings, self._client)
        self.groq = GroqEar(settings, self._client)
        self.local_whisper = LocalWhisperEar(settings)
        self.indic = IndicEar(settings)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @property
    def ready(self) -> bool:
        return any(e.ready for e in (self.gemini, self.deepgram, self.groq, self.local_whisper))

    @property
    def label(self) -> str:
        names = [e.label for e in (self.gemini, self.deepgram) if e.ready]
        if not names:
            names = [e.label for e in (self.groq, self.local_whisper) if e.ready][:1]
        return " + ".join(names)

    # ------------------------------------------------------------------ during the meeting

    async def hear(self, raw: bytes, mime_type: str, offset: float, ctx: EarContext, terms: list[str]) -> Hearing:
        pipeline = self._settings.pipeline
        prepared = audio_prep.prepare(raw, mime_type, offset)
        regions: list[Region] = vad.speech_regions(prepared.samples) if pipeline.voice_detection else []
        level = audio_prep.level(prepared.samples)
        hearing = Hearing(
            piece=ctx.piece,
            offset=offset,
            duration=prepared.duration_seconds,
            level=round(level, 4),
            speech=[(round(offset + r.start, 2), round(offset + r.end, 2)) for r in regions],
            prepared=prepared.prepared,
        )
        if prepared.samples is not None and prepared.samples.size and not regions and level < SILENT_LEVEL:
            hearing.skipped = True
            return hearing

        # Ears 1 + 2 in parallel. Gemini needs to give word times itself only when Deepgram cannot.
        timed_ctx = EarContext(**{**asdict(ctx), "need_timings": not self.deepgram.ready})
        jobs = {}
        if self.gemini.ready:
            jobs["gemini"] = self.gemini.hear(prepared, timed_ctx)
        if self.deepgram.ready:
            jobs["deepgram"] = self.deepgram.hear(prepared, ctx)
        results = await asyncio.gather(*jobs.values(), return_exceptions=True)
        for name, result in zip(jobs, results, strict=True):
            if isinstance(result, Hypothesis):
                hearing.hypotheses.append(result)
            else:
                hearing.failures.append(_failure(name, result))

        # Backups: Groq when an online ear failed or is not configured; local Whisper when nothing else worked.
        if len(hearing.hypotheses) < 2 and self.groq.ready:
            await self._try(hearing, "Groq Whisper", self.groq.hear(prepared, ctx))
        if not any(h.words for h in hearing.hypotheses) and self.local_whisper.ready and prepared.samples is not None:
            await self._try(hearing, "local Whisper", self.local_whisper.hear_span(prepared.samples, offset, ctx))

        await self._specialists(hearing, prepared.samples, offset, ctx, regions, terms)
        return hearing

    async def _try(self, hearing: Hearing, name: str, job: Any) -> None:
        try:
            hearing.hypotheses.append(await job)
        except (EarError, Exception) as exc:  # noqa: BLE001 - one ear never stops the meeting
            hearing.failures.append(_failure(name, exc))

    async def _specialists(
        self,
        hearing: Hearing,
        samples: np.ndarray | None,
        offset: float,
        ctx: EarContext,
        regions: list[Region],
        terms: list[str],
    ) -> None:
        """Layer 6: local models listen again, only where the online ears disagreed."""
        if samples is None or not (self.local_whisper.ready or self.indic.ready) or len(hearing.hypotheses) < 1:
            return
        words = self.vote(hearing, [], terms, regions_override=regions)
        doubt = self._settings.pipeline.doubt_below
        spans = _spans([(w.start, w.end) for w in words if w.confidence < doubt], offset, hearing.end)
        budget = SPECIALIST_MAX_SECONDS
        for start, end in spans:
            if budget <= 0:
                break
            budget -= end - start
            clip = samples[
                int((start - offset) * audio_prep.SAMPLE_RATE) : int((end - offset) * audio_prep.SAMPLE_RATE)
            ]
            if clip.size < audio_prep.SAMPLE_RATE // 2:
                continue
            text = " ".join(w.text for w in words if start <= w.start < end)
            hindi = ctx.language == "hi" or any(ord(c) in _DEVANAGARI for c in text)
            ear = self.indic if hindi and self.indic.ready else self.local_whisper if self.local_whisper.ready else None
            if ear is None:
                continue
            await self._try(hearing, ear.label, ear.hear_span(clip, start, ctx))

    # ------------------------------------------------------------------ fusing

    def vote(
        self,
        hearing: Hearing,
        captions: list[dict[str, Any]],
        terms: list[str],
        bot_name: str = "",
        regions_override: list[Region] | None = None,
    ) -> list[FusedWord]:
        """Layers 5 + 7 for one piece (no glossary fixes yet)."""
        regions = regions_override or [Region(a - hearing.offset, b - hearing.offset) for a, b in hearing.speech]
        hyps = [respread(h, regions, hearing.offset) for h in hearing.hypotheses]
        if captions and self._settings.pipeline.ears.captions:
            hyps.append(caption_hypothesis(captions, hearing.offset, max(hearing.end, hearing.offset + 1), bot_name))
        glossary = GlossaryFixer(terms, self._settings.pipeline.doubt_below).keys
        return fuse(hyps, (hearing.offset, max(hearing.end, hearing.offset + 1)), glossary)

    def assemble(
        self,
        hearings: list[Hearing],
        captions: list[dict[str, Any]],
        terms: list[str],
        bot_name: str = "",
        fixer: GlossaryFixer | None = None,
    ) -> list[FusedWord]:
        """All pieces -> one word list (overlaps removed), with glossary fixes (layer 8)."""
        ordered = sorted(hearings, key=lambda h: h.offset)
        words: list[FusedWord] = []
        for index, hearing in enumerate(ordered):
            piece_words = self.vote(hearing, captions, terms, bot_name)
            # Pieces overlap by a couple of seconds: keep each word once, cut in the middle of the overlap.
            lower = (ordered[index - 1].end + hearing.offset) / 2 if index > 0 else float("-inf")
            nxt = ordered[index + 1] if index + 1 < len(ordered) else None
            upper = (hearing.end + nxt.offset) / 2 if nxt and nxt.offset < hearing.end else float("inf")
            words.extend(w for w in piece_words if lower <= w.start < upper)
        if not ordered and captions:  # no audio at all: captions alone (never empty notes)
            last = max(float(c.get("end") or c["start"]) for c in captions) + 1
            words = fuse([caption_hypothesis(captions, 0, last, bot_name)], (0, last), set())
        if fixer is not None and self._settings.pipeline.glossary_fixer:
            words = fixer.fix(words)
        return words


def _spans(times: list[tuple[float, float]], low: float, high: float) -> list[tuple[float, float]]:
    spans: list[tuple[float, float]] = []
    for start, end in sorted(times):
        start, end = max(low, start - SPAN_PAD_SECONDS), min(high, end + SPAN_PAD_SECONDS)
        if spans and start <= spans[-1][1] + 1.0:
            spans[-1] = (spans[-1][0], max(spans[-1][1], end))
        else:
            spans.append((start, end))
    return spans


def _failure(name: str, exc: BaseException) -> str:
    detail = str(exc)[:160] or exc.__class__.__name__
    logger.warning("Ear %s failed: %s", name, detail)
    return f"{name} could not transcribe a piece ({detail})"
