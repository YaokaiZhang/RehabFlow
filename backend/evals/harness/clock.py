from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import RLock


class FakeMonotonicClock:
    """Deterministic monotonic clock for deadline and timeout tests."""

    def __init__(self, *, start: float = 0.0) -> None:
        self._value = float(start)
        self._base_datetime = datetime.now(timezone.utc)
        self._lock = RLock()

    def __call__(self) -> float:
        with self._lock:
            return self._value

    def now(self) -> float:
        return self()

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("fake clock cannot move backwards")
        with self._lock:
            self._value += float(seconds)

    def elapsed_since(self, started_at: float) -> int:
        return max(0, round((self() - started_at) * 1000))

    def now_datetime(self) -> datetime:
        return self._base_datetime + timedelta(seconds=self())
