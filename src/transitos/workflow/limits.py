"""Hard limits as code (CLAUDE.md rule 3). Never derived from model output."""
from __future__ import annotations

from transitos.contracts.objects import Block, Operator, Vehicle
from transitos.contracts.policy import Limits


def block_hours(block: Block) -> float:
    return (block.end - block.start).total_seconds() / 3600.0


class HardLimits:
    """Injected into the graph. Pure functions over canonical objects."""

    def __init__(self, limits: Limits | None = None):
        self.limits = limits or Limits()

    @property
    def min_spares(self) -> int:
        return self.limits.min_spares

    def operator_violations(self, op: Operator, block: Block) -> list[str]:
        out: list[str] = []
        if op.hours_today + block_hours(block) > self.limits.max_operator_hours_day:
            out.append(f"operator_hours:{op.id}")
        needs = block.needs
        if needs.type and needs.type not in op.qualifications:
            out.append(f"operator_qualification:{op.id}:{needs.type}")
        if needs.lift and not op.lift_trained:
            out.append(f"operator_lift_training:{op.id}")
        if op.available_from and op.available_from > block.start:
            out.append(f"operator_not_available:{op.id}")
        if op.available_to and op.available_to < block.end:
            out.append(f"operator_not_available:{op.id}")
        return out

    def spare_violations(self, v: Vehicle, block: Block) -> list[str]:
        out: list[str] = []
        if v.status != "ready":
            out.append(f"spare_not_ready:{v.id}")
        n = block.needs
        if v.seats < n.seats:
            out.append(f"spare_seats:{v.id}")
        if v.wheelchair_positions < n.wheelchair_positions:
            out.append(f"spare_wheelchair:{v.id}")
        if n.lift and not v.lift_ok:
            out.append(f"spare_lift:{v.id}")
        if n.type and v.type != n.type:
            out.append(f"spare_type:{v.id}")
        return out

    def spares_left_ok(self, ready_count: int, used: int) -> bool:
        """Last-spare rule: after using `used` spares at least min_spares must remain ready."""
        return ready_count - used >= self.limits.min_spares
