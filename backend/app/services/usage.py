"""Fair-use cap: audio seconds per extension install per UTC day.

Kept in memory on purpose: the Render free instance is stateless and may
restart, which only resets the counters (it never blocks a user wrongly).
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime


class UsageLimiter:
    def __init__(self, daily_cap_seconds: int) -> None:
        self._cap = daily_cap_seconds
        self._lock = threading.Lock()
        self._day = self._today()
        self._used: dict[str, float] = {}

    @staticmethod
    def _today() -> str:
        return datetime.now(UTC).strftime("%Y-%m-%d")

    def _roll_day(self) -> None:
        today = self._today()
        if today != self._day:
            self._day = today
            self._used.clear()

    def remaining(self, client_id: str) -> float:
        with self._lock:
            self._roll_day()
            return max(self._cap - self._used.get(client_id, 0.0), 0.0)

    def record(self, client_id: str, seconds: float) -> None:
        with self._lock:
            self._roll_day()
            self._used[client_id] = self._used.get(client_id, 0.0) + max(seconds, 0.0)
