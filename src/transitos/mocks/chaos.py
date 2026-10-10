"""Chaos switches. One Chaos object may be shared by several mocks or kept per mock.

timeout      all non-/sim routes answer 504
http_429     maintenance: all non-/sim routes answer 429 + Retry-After
duplicate    telematics: webhook/feed events delivered twice (same eventId)
late         telematics: events delayed 30 sim-min (out of order); alerts: visibility delay x6
reject_write scheduling reassign/unavailable/available and workforce assign answer 503
"""
from __future__ import annotations

import threading

MODES = ("timeout", "http_429", "duplicate", "late", "reject_write")


class Chaos:
    def __init__(self) -> None:
        self._on: set[str] = set()
        self._lock = threading.Lock()

    def set(self, mode: str, on: bool = True) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown chaos mode {mode!r}")
        with self._lock:
            (self._on.add if on else self._on.discard)(mode)

    def on(self, mode: str) -> bool:
        with self._lock:
            return mode in self._on

    def clear(self) -> None:
        with self._lock:
            self._on.clear()

    def active(self) -> list[str]:
        with self._lock:
            return sorted(self._on)
