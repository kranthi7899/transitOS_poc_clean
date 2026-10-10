"""Observation model: everything the invariant checkers need, as plain data.

A `World` (real or fake) returns an `Observation` after a scenario ran.  Invariants are pure
functions over it, so they can be tested without any workflow code.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from transitos.contracts.objects import Block, Operator, RunState, Vehicle, WorkOrder
from transitos.contracts.policy import AgencyPolicy

# set_vehicle_available is the separate, separately-approved return-to-service phase; it is not
# part of the plan saga (contracts.tools.SAGA_ORDER).
RETURN_ACTION = "set_vehicle_available"


class WriteRecord(BaseModel):
    """One write call as it reached a product (or its adapter)."""
    seq: int                       # global, monotonically increasing across writes/audit/approvals
    run_id: str
    action: str                    # a WriteTools method name
    key: str                       # idempotency key
    plan_version: int = 1
    params: dict[str, Any] = Field(default_factory=dict)
    ok: bool = True
    effect_id: str | None = None   # id of the thing created/changed (e.g. the WO id); same key => same effect
    compensation: bool = False     # True when issued by the saga to undo an earlier step
    compensates_key: str | None = None


class AuditEvent(BaseModel):
    seq: int
    kind: Literal["proposal", "approval", "rejection", "expiry", "write", "kill_switch", "escalation"]
    run_id: str | None = None
    plan_version: int | None = None
    key: str | None = None         # for kind=write: the write's idempotency key
    data: dict[str, Any] = Field(default_factory=dict)
    # approval: data["scope"] in {"plan", "return_to_service"}; kill_switch: data["on"] bool


class Observation(BaseModel):
    agency: str = "agency-a"
    policy: AgencyPolicy = Field(default_factory=lambda: AgencyPolicy(agency="agency-a"))
    runs: list[RunState] = Field(default_factory=list)
    writes: list[WriteRecord] = Field(default_factory=list)
    audit: list[AuditEvent] = Field(default_factory=list)
    # Final world state. Operator.hours_today / qualifications are the PRE-run values.
    vehicles: list[Vehicle] = Field(default_factory=list)
    blocks: list[Block] = Field(default_factory=list)
    operators: list[Operator] = Field(default_factory=list)
    work_orders: list[WorkOrder] = Field(default_factory=list)
    alerts: list[dict[str, Any]] = Field(default_factory=list)
    dead_letters: list[dict[str, Any]] = Field(default_factory=list)
    attached_events: dict[str, list[str]] = Field(default_factory=dict)  # run_id -> event ids attached as context

    def forward_writes(self, run_id: str | None = None) -> list[WriteRecord]:
        return [w for w in self.writes if not w.compensation and (run_id is None or w.run_id == run_id)]


class Violation(BaseModel):
    invariant: str
    run_id: str | None = None
    detail: str
