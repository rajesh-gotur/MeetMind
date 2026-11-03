"""Record-here mode: what the server learned while transcribing each piece, kept until the notes.

The extension uploads pieces during the meeting and asks for the notes at the end.
Between those calls the server remembers, per extension install, the doubtful
words (for the AI fixer) and the counts needed for the quality scores.
Kept in memory only, for at most MAX_AGE_SECONDS.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from app.pipeline.ai_fix import Doubt
from app.pipeline.fusion import FusedWord

MAX_AGE_SECONDS = 6 * 3600
MAX_DOUBTS = 2000


@dataclass
class _PieceStats:
    track: str
    received: float  # wall clock
    seconds: float
    covered: float
    words: int
    confident: int
    corrected: int
    ears: list[str]
    failures: list[str]


@dataclass
class ClientMemory:
    doubts: list[tuple[float, Doubt]] = field(default_factory=list)  # (wall clock, doubt)
    pieces: list[_PieceStats] = field(default_factory=list)


@dataclass(frozen=True)
class Recall:
    doubts: list[Doubt]
    meeting_seconds: float
    covered_seconds: float
    words: int
    confident: int
    corrected: int
    ears: list[str]
    failures: list[str]


class PieceMemory:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._clients: dict[str, ClientMemory] = {}

    def remember(
        self,
        client: str,
        track: str,
        words: list[FusedWord],
        doubt_below: float,
        seconds: float,
        covered: float,
        ears: list[str],
        failures: list[str],
    ) -> None:
        now = time.time()
        doubts = [
            (now, Doubt(time=w.start, heard=w.text, options=[w.text, *w.alternatives][:5]))
            for w in words
            if w.confidence < doubt_below and w.alternatives
        ]
        stats = _PieceStats(
            track=track,
            received=now,
            seconds=seconds,
            covered=covered,
            words=len(words),
            confident=sum(1 for w in words if w.agreement >= 2 or w.confidence >= 0.85),
            corrected=sum(1 for w in words if w.corrected),
            ears=ears,
            failures=failures,
        )
        with self._lock:
            self._expire(now)
            entry = self._clients.setdefault(client, ClientMemory())
            entry.doubts = (entry.doubts + doubts)[-MAX_DOUBTS:]
            entry.pieces.append(stats)

    def recall(self, client: str, since: float) -> Recall:
        """Everything remembered for this install since ``since`` (the meeting start, wall clock)."""
        with self._lock:
            entry = self._clients.get(client) or ClientMemory()
            doubts = [d for t, d in entry.doubts if t >= since]
            pieces = [p for p in entry.pieces if p.received >= since]
        # Each track (mic, call audio) covers the same meeting time: count the best-covered track.
        seconds: dict[str, float] = {}
        covered: dict[str, float] = {}
        for p in pieces:
            seconds[p.track] = seconds.get(p.track, 0.0) + p.seconds
            covered[p.track] = covered.get(p.track, 0.0) + p.covered
        return Recall(
            doubts=doubts,
            meeting_seconds=max(seconds.values(), default=0.0),
            covered_seconds=max(covered.values(), default=0.0),
            words=sum(p.words for p in pieces),
            confident=sum(p.confident for p in pieces),
            corrected=sum(p.corrected for p in pieces),
            ears=list(dict.fromkeys(e for p in pieces for e in p.ears)),
            failures=list(dict.fromkeys(f for p in pieces for f in p.failures)),
        )

    def _expire(self, now: float) -> None:
        for client in list(self._clients):
            entry = self._clients[client]
            entry.doubts = [(t, d) for t, d in entry.doubts if now - t < MAX_AGE_SECONDS]
            entry.pieces = [p for p in entry.pieces if now - p.received < MAX_AGE_SECONDS]
            if not entry.doubts and not entry.pieces:
                del self._clients[client]
