"""Policy contract (design §8). Hard limits are code, never model output."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Autonomy = Literal["recommend", "approve", "act"]


class Limits(BaseModel):
    min_spares: int = 1
    max_operator_hours_day: float = 10.0   # ASSUMED A6
    approval_timeout_min: int = 15
    pm_urgent_overdue_mi: int = 1000       # ASSUMED A7


class AgencyPolicy(BaseModel):
    agency: str
    kill_switch: bool = False
    autonomy: dict[str, Autonomy] = Field(default_factory=lambda: {
        "create_work_order": "approve", "set_vehicle_unavailable": "approve",
        "reassign_block": "approve", "assign_operator": "approve",
        "publish_rider_alert": "approve", "return_to_service": "approve"})
    limits: Limits = Field(default_factory=Limits)
