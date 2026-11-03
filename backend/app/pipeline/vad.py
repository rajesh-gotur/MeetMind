"""Layer 2 — voice detection (Silero VAD, MIT licence, bundled ONNX model).

Finds where people actually speak. MeetMind uses this to:
  * skip silent pieces (no engine is called, nothing is invented over silence),
  * know how much of the meeting has speech (quality score),
  * place words in time when an engine gives text without timings.

Runs on the CPU with onnxruntime (no PyTorch). Without onnxruntime a simple
loudness detector is used instead.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np

from app.pipeline.audio import SAMPLE_RATE

logger = logging.getLogger(__name__)

MODEL_PATH = Path(__file__).resolve().parent / "models" / "silero_vad.onnx"
WINDOW = 512  # samples per step at 16 kHz (32 ms), fixed by the model
CONTEXT = 64
START_THRESHOLD = 0.5
END_THRESHOLD = 0.35
MIN_SPEECH_SECONDS = 0.25
MIN_SILENCE_SECONDS = 0.45
PAD_SECONDS = 0.2


@dataclass(frozen=True)
class Region:
    start: float
    end: float

    @property
    def length(self) -> float:
        return self.end - self.start


class _Silero:
    def __init__(self, path: Path) -> None:
        import onnxruntime

        options = onnxruntime.SessionOptions()
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = 1
        self._session = onnxruntime.InferenceSession(
            str(path), sess_options=options, providers=["CPUExecutionProvider"]
        )

    def probabilities(self, samples: np.ndarray) -> np.ndarray:
        state = np.zeros((2, 1, 128), dtype=np.float32)
        context = np.zeros((1, CONTEXT), dtype=np.float32)
        rate = np.array(SAMPLE_RATE, dtype=np.int64)
        steps = samples.size // WINDOW
        out = np.zeros(steps, dtype=np.float32)
        for i in range(steps):
            chunk = samples[i * WINDOW : (i + 1) * WINDOW].reshape(1, -1).astype(np.float32)
            joined = np.concatenate([context, chunk], axis=1)
            prob, state = self._session.run(None, {"input": joined, "state": state, "sr": rate})
            out[i] = float(prob.reshape(-1)[0])
            context = joined[:, -CONTEXT:]
        return out


@lru_cache(maxsize=1)
def _model() -> _Silero | None:
    try:
        return _Silero(MODEL_PATH)
    except Exception as exc:  # noqa: BLE001 - onnxruntime missing or model unreadable
        logger.warning("Silero voice detection unavailable (%s); using a loudness detector", exc.__class__.__name__)
        return None


def engine_name() -> str:
    return "Silero VAD" if _model() is not None else "loudness detector"


def speech_regions(samples: np.ndarray | None) -> list[Region]:
    """Speech regions in seconds (relative to the start of the piece)."""
    if samples is None or samples.size < WINDOW:
        return []
    model = _model()
    probs = model.probabilities(samples) if model is not None else _energy(samples)
    return _regions(probs)


def speech_seconds(regions: list[Region]) -> float:
    return sum(r.length for r in regions)


def _energy(samples: np.ndarray) -> np.ndarray:
    steps = samples.size // WINDOW
    frames = samples[: steps * WINDOW].reshape(steps, WINDOW)
    rms = np.sqrt(np.mean(frames**2, axis=1))
    floor = max(float(np.percentile(rms, 20)) * 3, 0.01)
    return np.clip(rms / (floor * 2), 0, 1).astype(np.float32)


def _regions(probs: np.ndarray) -> list[Region]:
    step = WINDOW / SAMPLE_RATE
    regions: list[Region] = []
    speaking, start, silence_from = False, 0.0, None
    for i, p in enumerate(probs):
        t = i * step
        if not speaking and p >= START_THRESHOLD:
            speaking, start, silence_from = True, t, None
        elif speaking:
            if p < END_THRESHOLD:
                silence_from = silence_from if silence_from is not None else t
                if t - silence_from >= MIN_SILENCE_SECONDS:
                    regions.append(Region(start, silence_from))
                    speaking = False
            else:
                silence_from = None
    if speaking:
        regions.append(Region(start, len(probs) * step))
    total = len(probs) * step
    padded = [
        Region(max(0.0, r.start - PAD_SECONDS), min(total, r.end + PAD_SECONDS))
        for r in regions
        if r.length >= MIN_SPEECH_SECONDS
    ]
    merged: list[Region] = []
    for region in padded:
        if merged and region.start <= merged[-1].end:
            merged[-1] = Region(merged[-1].start, max(merged[-1].end, region.end))
        else:
            merged.append(region)
    return [Region(round(r.start, 2), round(r.end, 2)) for r in merged]
