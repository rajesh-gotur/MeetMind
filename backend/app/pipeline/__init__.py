"""MeetMind accuracy pipeline: every recording passes through these layers.

Transcript (per audio piece, then per meeting)
  L1  audio.py        clean audio (PyAV: mono, 16 kHz, steady loudness)
  L2  vad.py          voice detection (Silero VAD)
  L3  ears.py         Gemini 3.5 Transcribe with the glossary
  L4  ears.py         Deepgram Nova-3 (word timings, confidence)
  L5  captions.py     Meet captions (bot)
  L6  ears.py         optional local ears on doubtful parts (faster-whisper, IndicConformer)
  L7  fusion.py       word vote between all ears
  L8  glossary.py     glossary fixer (RapidFuzz + Jellyfish) + learned terms
  L9  ai_fix.py       AI chooses between the heard versions of doubtful words
  L10 speakers.py     speaker vote (captions, who-is-talking, voice groups)
Notes (services/notes.py)
  L11 facts.py        facts first, each quote verified, then the writer AI
  L12 verify.py       coverage rules, reviewer AI, calendar dates
Scores (scores.py) and saved meetings (store.py)
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from app.config import BACKEND_DIR, Settings
from app.pipeline.fusion import FusedWord
from app.pipeline.glossary import LearnedTerms
from app.schemas import TranscriptSegment, Word
from app.services.stt_common import unique_terms

SEGMENT_PAUSE_SECONDS = 0.8


@lru_cache(maxsize=1)
def learned_terms() -> LearnedTerms:
    return LearnedTerms(BACKEND_DIR / "learned_terms.json")


def glossary_terms(settings: Settings) -> list[str]:
    """config.yaml terms first, then terms learned in earlier meetings."""
    learned = learned_terms().load() if settings.vocabulary.learn else []
    return unique_terms(settings.vocabulary.terms, learned, limit=200)


def to_segments(words: list[FusedWord]) -> list[TranscriptSegment]:
    """Scored words -> phrases (split at pauses, sentence ends and voice changes)."""
    segments: list[TranscriptSegment] = []
    current: list[FusedWord] = []

    def flush() -> None:
        if not current:
            return
        voice = _voice_number(current[0].voice)
        segments.append(
            TranscriptSegment(
                start=round(current[0].start, 2),
                end=round(max(current[-1].end, current[0].start), 2),
                text=" ".join(w.text for w in current),
                speaker=voice,
                words=[
                    Word(
                        text=w.text,
                        start=round(w.start, 2),
                        end=round(max(w.end, w.start), 2),
                        speaker=_voice_number(w.voice),
                        confidence=round(w.confidence, 3),
                        alternatives=w.alternatives[:4],
                    )
                    for w in current
                ],
            )
        )
        current.clear()

    for word in words:
        if current and (
            word.start - current[-1].end > SEGMENT_PAUSE_SECONDS
            or word.voice != current[-1].voice
            or current[-1].text.endswith((".", "?", "!"))
        ):
            flush()
        current.append(word)
    flush()
    return segments


def _voice_number(voice: Any) -> int | None:
    """ "dg:3:1" -> 1 (the extension groups voices per piece itself)."""
    if not voice:
        return None
    try:
        return int(str(voice).rsplit(":", 1)[-1])
    except ValueError:
        return None
