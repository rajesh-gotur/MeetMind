"""Layer 1 — audio preparation.

Every recorded piece (WebM/Opus from the browser) is decoded with PyAV (bundled
FFmpeg), mixed down to one channel, resampled to 16 kHz and brought to a steady
loudness. The result is encoded once as FLAC, which every speech engine accepts,
and also kept as raw samples for voice detection (layer 2).

If PyAV is not installed or the audio cannot be decoded, the original bytes are
passed through unchanged, so transcription still works (only less polished).
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass

import numpy as np

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
TARGET_RMS = 0.08  # about -22 dBFS: comfortable speech level for every engine
MAX_GAIN = 12.0  # never boost very quiet audio more than this (it would mostly be noise)
PEAK_LIMIT = 0.97


@dataclass(frozen=True)
class PreparedAudio:
    """One piece of audio, ready for the ears."""

    data: bytes  # bytes sent to the engines
    mime_type: str
    offset_seconds: float  # where this piece starts in the meeting
    duration_seconds: float
    samples: np.ndarray | None = None  # mono float32 at 16 kHz, when decoding worked
    prepared: bool = False  # True when layer 1 ran (False: original bytes passed through)

    @property
    def filename(self) -> str:
        return "piece.flac" if self.mime_type == "audio/flac" else "piece.webm"


def prepare(raw: bytes, mime_type: str, offset_seconds: float) -> PreparedAudio:
    """Decodes, cleans and re-encodes one piece. Never raises: falls back to the original bytes."""
    try:
        samples = decode(raw)
    except Exception as exc:  # noqa: BLE001 - any codec problem: keep the original audio
        logger.warning("Audio preparation skipped (%s): using the original audio", exc.__class__.__name__)
        return PreparedAudio(raw, mime_type, offset_seconds, duration_seconds=0.0)
    if samples.size == 0:
        return PreparedAudio(raw, mime_type, offset_seconds, duration_seconds=0.0, samples=samples)
    cleaned = normalize(samples)
    try:
        data = encode_flac(cleaned)
    except Exception as exc:  # noqa: BLE001 - encoding problem: keep the original bytes, still use samples
        logger.warning("FLAC encoding failed (%s): sending the original audio", exc.__class__.__name__)
        return PreparedAudio(raw, mime_type, offset_seconds, cleaned.size / SAMPLE_RATE, cleaned)
    return PreparedAudio(data, "audio/flac", offset_seconds, cleaned.size / SAMPLE_RATE, cleaned, prepared=True)


def decode(raw: bytes) -> np.ndarray:
    """Any browser audio (WebM/Opus, Ogg, WAV, MP3, MP4) -> mono float32 samples at 16 kHz."""
    import av  # PyAV: imported lazily so the server starts without it

    chunks: list[np.ndarray] = []
    with av.open(io.BytesIO(raw), mode="r") as container:
        if not container.streams.audio:
            return np.zeros(0, dtype=np.float32)
        resampler = av.AudioResampler(format="flt", layout="mono", rate=SAMPLE_RATE)
        for frame in container.decode(audio=0):
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray().reshape(-1))
        for out in resampler.resample(None):  # flush
            chunks.append(out.to_ndarray().reshape(-1))
    return np.concatenate(chunks).astype(np.float32) if chunks else np.zeros(0, dtype=np.float32)


def normalize(samples: np.ndarray) -> np.ndarray:
    """Steady loudness: raise quiet speech, never clip loud speech."""
    voiced = samples[np.abs(samples) > 0.01]
    rms = float(np.sqrt(np.mean(voiced**2))) if voiced.size else 0.0
    if rms <= 1e-5:
        return samples
    gain = min(TARGET_RMS / rms, MAX_GAIN)
    out = samples * gain
    peak = float(np.max(np.abs(out)))
    if peak > PEAK_LIMIT:
        out *= PEAK_LIMIT / peak
    return out.astype(np.float32)


def encode_flac(samples: np.ndarray) -> bytes:
    import av

    buffer = io.BytesIO()
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
    with av.open(buffer, mode="w", format="flac") as container:
        stream = container.add_stream("flac", rate=SAMPLE_RATE, layout="mono")
        frame_size = 4096
        for start in range(0, pcm.size, frame_size):
            block = pcm[start : start + frame_size].reshape(1, -1)
            frame = av.AudioFrame.from_ndarray(block, format="s16", layout="mono")
            frame.sample_rate = SAMPLE_RATE
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    return buffer.getvalue()


def level(samples: np.ndarray | None) -> float:
    """Loudest short-term level (0-1), used to tell silence from speech."""
    if samples is None or samples.size == 0:
        return 0.0
    window = SAMPLE_RATE // 4
    usable = samples[: samples.size - samples.size % window] if samples.size >= window else samples
    if usable.size < window:
        return float(np.sqrt(np.mean(usable**2)))
    return float(np.max(np.sqrt(np.mean(usable.reshape(-1, window) ** 2, axis=1))))


def available() -> bool:
    try:
        import av  # noqa: F401
    except ImportError:
        return False
    return True
