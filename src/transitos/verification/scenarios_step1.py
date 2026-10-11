"""Phase 10 Step 1 scenarios: qualification, hours, spare reuse, graded vehicle fit, missing data.

Same baseline as `scenarios.py` (agency-a, veh-1 on blk-1, ready spares, OP-1 assigned, OP-2 extra board), with
extra knobs in `Step1Tweaks`. The integration world and the FakeWorld both read the knobs with `getattr`, so the
original `Tweaks` keep working. New script op: ("ack", [clearance rule ids]) records the approver's
acknowledgement that the next ("approve", "plan") carries.
"""
from __future__ import annotations

from dataclasses import dataclass

from .scenarios import Outcome, Scenario, Tweaks, _s

_PROPOSE_ONLY = (("deliver", 0),)
_FULL = (("deliver", 0), ("approve", "plan"), ("advance", 72), ("close_wo",), ("approve", "return_to_service"))


@dataclass(frozen=True)
class Step1Tweaks(Tweaks):
    spare_type: str = "standard"
    spare_length_ft: float | None = None            # None: the vehicle record carries the class's typical length
    block_type: str = "standard"
    block_max_length_ft: float | None = None        # declared block restriction
    second_block: bool = False                      # a second block on the faulty vehicle (2h, operator OP-2)
    second_block_start_h: float = 6.0               # hours after T0; blk-1 runs 1h..5h, so 6.0 does not overlap
    operator_licence_class: str = "B"
    operator_endorsements: tuple[str, ...] = ("P",)
    operator_trained_types: tuple[str, ...] = ("standard",)
    operator_hours_week: float = 20.0
    credential_days: tuple[tuple[str, int], ...] = ()   # (credential, days from the scenario day it expires)
    drop_operator_fields: tuple[str, ...] = ()      # workforce export columns the product does not supply
    vehicle_fit: str = "allow_larger"               # agency setting


_ARTIC_TRAINED = ("standard", "articulated")
# the operators of half_operator_fields_missing lose these export columns (7 of the 15)
HALF_DROPPED = ("qualifications", "licence_class", "endorsements", "trained_vehicle_types", "training_valid_to",
                "hours_week", "medical_valid_to")

STEP1_SCENARIOS: tuple[Scenario, ...] = (
    _s("expired_training", "Vehicle-type training expires on the block's day -> no operator qualifies, escalate",
       Outcome.ESCALATE_NO_SAFE_OPTION, _PROPOSE_ONLY,
       tweaks=Step1Tweaks(credential_days=(("standard", 0),)), expect_wo_creates=0),
    _s("endorsement_missing", "No operator holds the passenger endorsement -> escalate",
       Outcome.ESCALATE_NO_SAFE_OPTION, _PROPOSE_ONLY, tweaks=Step1Tweaks(operator_endorsements=()),
       expect_wo_creates=0),
    _s("spare_reused_across_blocks", "Two non-overlapping blocks, two ready spares, min_spares 1: one spare serves "
       "both (distinct spares consumed = 1), so the plan is valid",
       Outcome.PLAN_VALID, _FULL, tweaks=Step1Tweaks(second_block=True), expect_wo_creates=1),
    _s("weekly_hours_cap", "Every operator is at 68h in the 8-day window; a 4h block breaks the 70h cap -> escalate",
       Outcome.ESCALATE_NO_SAFE_OPTION, _PROPOSE_ONLY, tweaks=Step1Tweaks(operator_hours_week=68.0),
       expect_wo_creates=0),
    _s("larger_class_fallback", "No exact-type spare exists; articulated spares (larger class) are the fallback "
       "tier: allowed only after the approver acknowledges the vehicle_fit warning",
       Outcome.PLAN_VALID, (("deliver", 0), ("ack", ["vehicle_fit"]), ("approve", "plan"), ("advance", 72),
                            ("close_wo",), ("approve", "return_to_service")),
       tweaks=Step1Tweaks(spare_type="articulated", operator_trained_types=_ARTIC_TRAINED), expect_wo_creates=1),
    _s("length_restriction_blocks_larger", "The block declares a 45 ft limit; the only spares are 60 ft "
       "articulated -> hard block, escalate",
       Outcome.ESCALATE_NO_SAFE_OPTION, _PROPOSE_ONLY,
       tweaks=Step1Tweaks(spare_type="articulated", operator_trained_types=_ARTIC_TRAINED,
                          block_max_length_ft=45.0), expect_wo_creates=0),
    _s("half_operator_fields_missing", "Half the operator fields are missing from the workforce export: the plan "
       "stays on the list as unverifiable and runs to completion once the approver acknowledges those items",
       Outcome.PLAN_VALID, (("deliver", 0), ("ack", ["operator_qualification", "operator_hours_week"]),
                            ("approve", "plan"), ("advance", 72), ("close_wo",), ("approve", "return_to_service")),
       tweaks=Step1Tweaks(drop_operator_fields=HALF_DROPPED), expect_wo_creates=1),
    _s("vehicle_fit_exact_excludes_larger", "Agency vehicle_fit is 'exact' and only larger-class spares exist "
       "-> excluded, escalate",
       Outcome.ESCALATE_NO_SAFE_OPTION, _PROPOSE_ONLY,
       tweaks=Step1Tweaks(spare_type="articulated", operator_trained_types=_ARTIC_TRAINED, vehicle_fit="exact"),
       expect_wo_creates=0),
)
