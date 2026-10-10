"""Shared simulated clock. All mocks and tests use this, never wall time."""
from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

DEFAULT_START = datetime(2026, 1, 5, 6, 0, tzinfo=UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class SimClock:
    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or DEFAULT_START
        self._lock = threading.Lock()

    def now(self) -> datetime:
        with self._lock:
            return self._now

    def advance(self, hours: float = 0.0, minutes: float = 0.0) -> datetime:
        with self._lock:
            self._now = self._now + timedelta(hours=hours, minutes=minutes)
            return self._now

    def iso(self) -> str:
        return iso(self.now())
