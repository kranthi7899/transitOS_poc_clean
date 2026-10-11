"""Canonical objects (design §2.4) and workflow state (§2.7)."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from .clearance import ClearanceItem

VehicleStatus = Literal["ready", "in_service", "out_of_service", "reserved"]


class Needs(BaseModel):
    seats: int = 0
    wheelchair_positions: int = 0
    lift: bool = False
    type: str | None = None
    max_vehicle_length_ft: float | None = None  # v1.2 (C-1): a declared block restriction


class Vehicle(BaseModel):
    id: str
    agency: str
    seats: int
    wheelchair_positions: int = 0
    lift_ok: bool = False
    type: str = "bus"
    status: VehicleStatus = "ready"
    depot: str = ""
    ev_soc: float | None = None
    length_ft: float | None = None  # v1.2 (C-1)


class Block(BaseModel):
    id: str
    vehicle_id: str
    operator_id: str | None = None
    start: datetime
    end: datetime
    start_depot: str = ""
    needs: Needs = Field(default_factory=Needs)


class Operator(BaseModel):
    id: str
    qualifications: list[str] = Field(default_factory=list)
    lift_trained: bool = False
    hours_today: float = 0.0
    hours_week: float = 0.0
    available_from: datetime | None = None
    available_to: datetime | None = None
    on_extra_board: bool = False
    # v1.2 (D-1): qualification model; `qualifications` is legacy and ignored when these are set
    licence_class: Literal["A", "B", "C"] | None = None
    endorsements: list[str] = Field(default_factory=list)
    trained_vehicle_types: list[str] = Field(default_factory=list)
    training_valid_to: dict[str, datetime] = Field(default_factory=dict)
    medical_valid_to: datetime | None = None
    last_duty_end: datetime | None = None
    duty_start_today: datetime | None = None


class WorkOrder(BaseModel):
    id: str
    native_no: str
    asset_vehicle_id: str
    status: Literal["open", "in_progress", "closed"] = "open"
    description: str = ""
    opened_at: datetime | None = None
    closed_at: datetime | None = None
    source: Literal["system", "human"] = "system"


ActionName = Literal[
    "create_work_order", "set_vehicle_unavailable", "reassign_block",
    "assign_operator", "publish_rider_alert", "set_vehicle_available",
    "cancel_work_order",
]
StepStatus = Literal["pending", "done", "failed", "compensated"]


class Step(BaseModel):
    action: ActionName
    params: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str
    compensation: ActionName | None = None
    status: StepStatus = "pending"


class Plan(BaseModel):
    decision: Literal["pull_now", "pull_at_block_end", "monitor"]
    pull_at: datetime | None = None
    steps: list[Step] = Field(default_factory=list)
    reasoning: str = ""
    alternatives: list[dict[str, Any]] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    fallback_used: bool = False  # True when code-ranked fallback replaced model output
    clearance: list[ClearanceItem] = Field(default_factory=list)  # v1.2 (C-1)
    rank_reason: str = ""
    vehicle_fit: str | None = None


class RunState(BaseModel):
    run_id: str
    agency: str
    vehicle_id: str
    trigger_event: dict[str, Any]
    status: str = "STARTED"
    facts: dict[str, Any] = Field(default_factory=dict)
    violations: list[str] = Field(default_factory=list)
    plan: Plan | None = None
    plan_version: int = 1
    decision: Literal["approve", "reject", "edit", "expired"] | None = None
    decided_by: str | None = None
    decided_at: datetime | None = None
    steps_done: list[Step] = Field(default_factory=list)
    compensations_done: list[Step] = Field(default_factory=list)
    wo_id: str | None = None
    audit_refs: list[str] = Field(default_factory=list)
    acknowledged: list[str] = Field(default_factory=list)  # v1.2 (C-1): rule ids the approver acknowledged


RUN_STATES = [
    "STARTED", "GATHERING", "PROPOSED", "AWAITING_APPROVAL", "REVALIDATING",
    "EXECUTING", "WAITING_FOR_REPAIR", "RETURNING", "DONE",
    "CLOSED_REJECTED", "COMPENSATING", "ESCALATED",
]


class DecisionRecord(BaseModel):
    """Audit record (design §2.8)."""
    run_id: str
    plan_version: int
    trigger_event_id: str
    facts_digest: dict[str, Any] = Field(default_factory=dict)
    model: str = "stub"
    prompt_version: str = "v0"
    recommendation: dict[str, Any] = Field(default_factory=dict)
    human_action: dict[str, Any] = Field(default_factory=dict)
    actions: list[dict[str, Any]] = Field(default_factory=list)
    outcome: Literal["done", "compensated", "escalated", "rejected", "pending"] = "pending"
    clearance: list[dict[str, Any]] = Field(default_factory=list)  # v1.2 (C-1)
    acknowledged: list[str] = Field(default_factory=list)
    vehicle_fit: str | None = None
