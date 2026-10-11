"""Crosswalk contract (design §2.5, §5)."""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel

from .envelope import CloudEvent


class CrosswalkEntry(BaseModel):
    agency: str
    product: str
    native_id: str
    entity: str  # vehicle | operator | asset
    canonical_id: str
    # v1.2 (B-1): validity window (valid_from inclusive, valid_to exclusive) and identity evidence
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    vin: str | None = None
    plate: str | None = None
    display_name: str | None = None


ResolutionOutcome = Literal["MATCH", "AMBIGUOUS", "NO_MATCH"]


class Candidate(BaseModel):
    canonical_id: str
    score: float
    evidence: list[str] = []


class Resolution(BaseModel):
    outcome: ResolutionOutcome
    confidence: float
    candidates: list[Candidate] = []


class Crosswalk(Protocol):
    def lookup(self, agency: str, product: str, native_id: str, at: datetime | None = None) -> str | None: ...
    def candidates(self, agency: str, product: str, native_id: str,
                   at: datetime | None = None) -> list[CrosswalkEntry]: ...
    def all_ids(self, agency: str, canonical_id: str) -> dict[str, str]: ...
    def add_unresolved(self, agency: str, product: str, native_id: str, event_id: str) -> None: ...
    def resolve(self, entry: CrosswalkEntry) -> list[CloudEvent]:
        """Add a mapping; return parked events to republish to events.raw."""
        ...
