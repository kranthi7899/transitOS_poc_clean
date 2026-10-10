"""Ports the panel depends on. Real wiring happens at integration; the panel imports no other track."""
from __future__ import annotations

from typing import Any, Protocol

from transitos.contracts.envelope import CloudEvent
from transitos.contracts.objects import DecisionRecord, RunState


class RunNotFound(Exception):
    pass


class StaleVersion(Exception):
    """Decision targeted a plan_version that is no longer current."""


class DecisionConflict(Exception):
    """Run already decided differently."""


class RunGateway(Protocol):
    def list_pending(self, agency: str | None = None) -> list[RunState]: ...
    def get_run(self, run_id: str) -> RunState | None: ...
    def submit_decision(self, run_id: str, decision: str, plan_version: int,
                        edits: dict[str, Any] | None, user: str) -> RunState:
        """Resume the run's interrupt. May raise RunNotFound, StaleVersion, DecisionConflict."""
        ...


class EventSink(Protocol):
    def publish(self, event: CloudEvent) -> None:
        """Publish to events.raw."""
        ...


class AuditReader(Protocol):
    def for_run(self, run_id: str) -> list[DecisionRecord]: ...
    def timeline(self, agency: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """Newest-last list of {time, run_id, kind, summary}."""
        ...


class KillSwitchStore(Protocol):
    def is_on(self, agency: str) -> bool: ...
    def set(self, agency: str, on: bool) -> None: ...


class UnresolvedReader(Protocol):
    """Crosswalk contract has no listing method; this read port fills that gap (see handoff)."""
    def list_unresolved(self, agency: str | None = None) -> list[dict[str, Any]]:
        """Items: {agency, product, native_id, event_ids}."""
        ...
