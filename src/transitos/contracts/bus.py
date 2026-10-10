"""Bus interface. Track B implements an in-process/SQLite bus (local profile) and a Kafka bus (full profile).
Semantics every implementation must give: ordered per key, at-least-once, consumer-owned offsets, replay."""
from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol

from .envelope import CloudEvent


class Producer(Protocol):
    def send(self, topic: str, key: str, event: CloudEvent) -> None: ...
    def flush(self) -> None: ...


class Consumer(Protocol):
    def poll(self, topic: str, group: str, limit: int = 100) -> Iterator[tuple[int, CloudEvent]]:
        """Yield (offset, event) after the group's committed offset, in order per key."""
        ...

    def commit(self, topic: str, group: str, offset: int) -> None:
        """Commit only after side effects are durable."""
        ...
