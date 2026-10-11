"""Hard limits as code (CLAUDE.md rule 3). Never derived from model output.

Phase 10: the rules live in `transitos.clearance` (one implementation). `HardLimits` keeps its public names
as thin wrappers so callers and tests written against them stay valid; the graph itself calls clearance
directly with real facts (capability profiles, schedule feed). The wrappers judge with a fully native legacy
profile, i.e. they behave as the pre-Phase-10 checks did (qualification = block type in the legacy list)."""
from __future__ import annotations

from transitos.clearance.evaluate import operator_items, vehicle_items
from transitos.clearance.facts import Facts, native_profile
from transitos.clearance.rules import guarded, known_need_type, last_spare_item
from transitos.contracts.objects import Block, Operator, Vehicle
from transitos.contracts.policy import AgencyPolicy, Limits

_LEGACY_DROP = ("licence_class", "endorsements", "trained_vehicle_types", "training_valid_to",
                "medical_valid_to", "duty_times")


def block_hours(block: Block) -> float:
    return (block.end - block.start).total_seconds() / 3600.0


class HardLimits:
    """Injected into the graph. Pure functions over canonical objects."""

    def __init__(self, limits: Limits | None = None):
        self.limits = limits or Limits()

    @property
    def min_spares(self) -> int:
        return self.limits.min_spares

    def _facts(self) -> Facts:
        return Facts(blocks={}, vehicles={}, operators={}, ready_count=None, commitments=None,
                     profiles={"scheduling": native_profile("scheduling"),
                               "workforce": native_profile("workforce", drop=_LEGACY_DROP)})

    def _policy(self) -> AgencyPolicy:
        return AgencyPolicy(agency="-", limits=self.limits)

    def operator_violations(self, op: Operator, block: Block) -> list[str]:
        items = guarded(f"operator {op.id}", operator_items, block, op, known_need_type(block), self._facts(), self._policy())
        return [f"{i.rule}:{op.id}" for i in items if i.status == "violation"]

    def spare_violations(self, v: Vehicle, block: Block) -> list[str]:
        items = guarded(f"vehicle {v.id}", vehicle_items, block, v, self._facts(), self._policy())
        return [f"{i.rule}:{v.id}" for i in items if i.status == "violation"]

    def spares_left_ok(self, ready_count: int, used: int) -> bool:
        """Last-spare rule: after using `used` distinct spares at least min_spares must remain ready."""
        return last_spare_item(ready_count, used, self.limits.min_spares).status != "violation"
