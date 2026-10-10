"""Crosswalk contract (design §2.5, §5)."""
from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel

from .envelope import CloudEvent


class CrosswalkEntry(BaseModel):
    agency: str
    product: str
    native_id: str
    entity: str  # vehicle | operator | asset
    canonical_id: str


class Crosswalk(Protocol):
    def lookup(self, agency: str, product: str, native_id: str) -> str | None: ...
    def all_ids(self, agency: str, canonical_id: str) -> dict[str, str]: ...
    def add_unresolved(self, agency: str, product: str, native_id: str, event_id: str) -> None: ...
    def resolve(self, entry: CrosswalkEntry) -> list[CloudEvent]:
        """Add a mapping; return parked events to republish to events.raw."""
        ...
