"""The World interface: what the verification harness needs from the integrated system.

The integration lead provides a real implementation (all mocks + plumbing + tools + workflow wired
in-process).  `fake_world.FakeWorld` is a behavioural stand-in used to prove the invariants.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from transitos.contracts.envelope import CloudEvent

from .model import Observation

if TYPE_CHECKING:
    from .scenarios import Scenario


@runtime_checkable
class World(Protocol):
    def apply(self, scenario: Scenario) -> None:
        """Reset to a fresh synthetic fleet, apply scenario.tweaks and scenario.chaos."""

    def deliver(self, event: CloudEvent) -> None:
        """Hand an event to the pipeline (as if published by its mock) and process to quiescence
        (i.e. until the run blocks on approval / repair or terminates)."""

    def approve(self, run_id: str, scope: str = "plan", by: str = "dispatcher") -> None:
        """Human approves. scope "plan" for the proposed plan, "return_to_service" for the return step."""

    def reject(self, run_id: str, by: str = "dispatcher") -> None: ...

    def advance_clock(self, hours: float) -> None:
        """Advance the sim clock; fires approval timeouts. Process to quiescence."""

    def set_kill_switch(self, on: bool) -> None: ...

    def acknowledge(self, run_id: str, rules: list[str]) -> None:
        """The approver acknowledges these clearance rule ids; the next plan approval carries them
        (execution waits for every requires_ack item to be acknowledged)."""

    def close_work_order(self, run_id: str) -> None:
        """Simulate the shop closing the WO (emits WorkOrderClosed); process to quiescence."""

    def take_spare(self, run_id: str) -> None:
        """Another actor grabs the spare the run proposed (scheduling status change), after the proposal."""

    def pick(self, canonical_id: str) -> None:
        """Phase 10 Step 2: a human maps the first parked native id to this canonical vehicle (panel POST
        /crosswalk); the parked events are republished. Process to quiescence."""

    def observe(self) -> Observation: ...
