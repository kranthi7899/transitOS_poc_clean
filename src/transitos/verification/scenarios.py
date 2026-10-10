"""Scenario definitions as data.

A scenario = trigger event(s) + seeded-world tweaks + chaos switches + a human/clock script +
the expected outcome class.  The runner (`runner.py`) interprets it against any `World`.

Baseline world (before tweaks), all in agency "agency-a":
  veh-1 (native T-77 / 1106 / BUS-44) in service on blk-1, operator OP-1; block needs 30 seats,
  1 wheelchair position, lift.  Two ready spares veh-2, veh-3 (full capability).  OP-1 (assigned) and
  OP-2 (extra board) qualified and lift-trained, hours_today=4.  Block length 4h.  min_spares=1,
  max_operator_hours_day=10 => using one of two spares is allowed, using the last one is not.
  The block keeps its operator unless that operator fails a hard limit (then assign_operator is a plan
  step); so the baseline plan has four writes and `operator_swap_needed` has all five.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from transitos.contracts import ids
from transitos.contracts.envelope import CloudEvent

AGENCY = "agency-a"
NATIVE_TELEMATICS = "T-77"
NATIVE_ASSET = "1106"
T0 = datetime(2026, 10, 10, 6, 0, tzinfo=UTC)


class Outcome(str, Enum):
    PLAN_VALID = "plan-valid"                      # proposed, approved, executed, (returned to service)
    MONITOR = "monitor"                            # no action needed now
    ESCALATE_NO_SAFE_OPTION = "escalate-no-safe-option"  # hard limit / no feasible option; zero writes
    ESCALATE_DEGRADED = "escalate-degraded"        # a product was unavailable on read; zero writes
    REJECTED = "rejected"                          # human rejected; zero writes
    EXPIRED_ESCALATED = "expired-escalated"        # approval timed out; zero writes
    COMPENSATED_ESCALATED = "compensated-escalated"  # a write failed mid-plan; undone in reverse; escalated
    HALTED_KILL_SWITCH = "halted-kill-switch"      # kill switch stopped the run; no writes afterwards
    DEAD_LETTER = "dead-letter"                    # unresolved vehicle id; no run


@dataclass(frozen=True)
class Tweaks:
    """Seeded-world tweaks (all optional). Interpreted by World.apply."""
    spares: int = 2                       # ready spare vehicles
    spare_wheelchair_positions: int = 2   # on every spare
    spare_lift_ok: bool = True            # on every spare
    spare_seats: int = 40
    block_wheelchair_positions: int = 1
    block_needs_lift: bool = True
    operator_hours_today: float = 4.0     # applied to every operator
    assigned_operator_hours_today: float | None = None  # override for the block's assigned operator (OP-1)
    operators_qualified: bool = True      # False => no operator holds the block's required qualification
    operators_lift_trained: bool = True
    human_wo_exists: bool = False         # an open human-opened WO already exists for the vehicle
    pm_overdue_mi: int = 1200             # for PMOverdue triggers


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    events: tuple[CloudEvent, ...]
    expected: Outcome
    script: tuple[tuple[str, Any], ...]   # ("deliver", idx) ("approve", scope) ("reject",) ("advance", hours)
                                          # ("kill_switch", bool) ("close_wo",) ("take_spare",)
    tweaks: Tweaks = field(default_factory=Tweaks)
    chaos: dict[str, Any] = field(default_factory=dict)
    expect_runs: int = 1
    expect_wo_creates: int | None = None  # None = do not check
    expect_plan_version: int | None = None
    expect_attached: int = 0
    note: str = ""


# ---- event builders -------------------------------------------------------------------------------
def _ev(etype: str, eid: str, data: dict[str, Any], product: str, minutes: int = 0) -> CloudEvent:
    return CloudEvent(
        id=eid, source=f"adapter/{product}/{AGENCY}", type=etype,  # type: ignore[arg-type]
        time=T0.replace(minute=minutes), agencyid=AGENCY, data=data)


def fault_event(n: int = 1, native: str = NATIVE_TELEMATICS, stop: bool = True) -> CloudEvent:
    spn = 100 + n
    return _ev("VehicleFaultDetected", ids.fault_id(AGENCY, native, spn, 1, f"t{n}"),
               {"native_vehicle_id": native, "spn": spn, "fmi": 1, "lamps": {"stop": stop, "warn": True},
                "occurrence_count": 3}, "telematics", n)


def pm_event(overdue_mi: int = 1200) -> CloudEvent:
    due = 10000
    return _ev("PMOverdue", ids.pm_id(AGENCY, NATIVE_ASSET, "PM-A", due),
               {"native_asset_id": NATIVE_ASSET, "service_code": "PM-A", "due_mi": due,
                "meter_mi": due + overdue_mi}, "maintenance")


def dvir_event() -> CloudEvent:
    text = "brake pedal soft, air leak at rear axle"
    return _ev("DefectReported", ids.defect_id(AGENCY, NATIVE_TELEMATICS, "t1", hashlib.sha1(text.encode()).hexdigest()[:8]),
               {"native_vehicle_id": NATIVE_TELEMATICS, "defect_text": text, "reported_by_role": "operator"},
               "telematics")


# ---- scripts --------------------------------------------------------------------------------------
_DELIVER_APPROVE = (("deliver", 0), ("approve", "plan"))
_FULL = (("deliver", 0), ("approve", "plan"), ("advance", 72), ("close_wo",), ("approve", "return_to_service"))
_PROPOSE_ONLY = (("deliver", 0),)


def _s(id_: str, title: str, expected: Outcome, script, events=None, **kw) -> Scenario:
    return Scenario(id=id_, title=title, events=tuple(events or (fault_event(),)), expected=expected,
                    script=tuple(script), **kw)


SCENARIOS: tuple[Scenario, ...] = (
    _s("severe_fault_with_spare", "Severe fault, spare available, full happy path",
       Outcome.PLAN_VALID, _FULL, expect_wo_creates=1),
    _s("operator_swap_needed", "Assigned operator would exceed max hours -> all five saga steps incl. operator swap",
       Outcome.PLAN_VALID, _FULL, tweaks=Tweaks(assigned_operator_hours_today=7.0), expect_wo_creates=1),
    _s("pm_overdue_urgent", "PM 1200 mi overdue (urgent) -> plan",
       Outcome.PLAN_VALID, _FULL, events=[pm_event(1200)], expect_wo_creates=1),
    _s("pm_overdue_minor", "PM 200 mi overdue (not urgent) -> monitor, no writes",
       Outcome.MONITOR, _PROPOSE_ONLY, events=[pm_event(200)], tweaks=Tweaks(pm_overdue_mi=200),
       expect_wo_creates=0),
    _s("dvir_defect", "Operator DVIR defect -> plan",
       Outcome.PLAN_VALID, _FULL, events=[dvir_event()], expect_wo_creates=1),
    _s("last_spare", "Only one spare: using it would breach min_spares -> escalate",
       Outcome.ESCALATE_NO_SAFE_OPTION, _PROPOSE_ONLY, tweaks=Tweaks(spares=1), expect_wo_creates=0),
    _s("wheelchair_mismatch", "Spares lack wheelchair positions the block needs -> escalate",
       Outcome.ESCALATE_NO_SAFE_OPTION, _PROPOSE_ONLY,
       tweaks=Tweaks(spare_wheelchair_positions=0), expect_wo_creates=0),
    _s("lift_mismatch", "Spares have no working lift, block needs lift -> escalate",
       Outcome.ESCALATE_NO_SAFE_OPTION, _PROPOSE_ONLY, tweaks=Tweaks(spare_lift_ok=False), expect_wo_creates=0),
    _s("operator_hours_exhausted", "All eligible operators would exceed max hours -> escalate",
       Outcome.ESCALATE_NO_SAFE_OPTION, _PROPOSE_ONLY, tweaks=Tweaks(operator_hours_today=8.0),
       expect_wo_creates=0),
    _s("unqualified_operator", "No operator holds required qualification -> escalate",
       Outcome.ESCALATE_NO_SAFE_OPTION, _PROPOSE_ONLY, tweaks=Tweaks(operators_qualified=False),
       expect_wo_creates=0),
    _s("lift_untrained_operator", "No lift-trained operator for a lift block -> escalate",
       Outcome.ESCALATE_NO_SAFE_OPTION, _PROPOSE_ONLY, tweaks=Tweaks(operators_lift_trained=False),
       expect_wo_creates=0),
    _s("no_feasible_spare", "No spare vehicles at all -> escalate",
       Outcome.ESCALATE_NO_SAFE_OPTION, _PROPOSE_ONLY, tweaks=Tweaks(spares=0), expect_wo_creates=0),
    _s("duplicate_trigger", "Same fault event delivered three times -> one run, one WO",
       Outcome.PLAN_VALID, (("deliver", 0), ("deliver", 0), ("deliver", 0), ("approve", "plan")),
       expect_wo_creates=1),
    _s("trigger_vehicle_active_run", "Second distinct fault on a vehicle with an active run -> attached as context",
       Outcome.PLAN_VALID, (("deliver", 0), ("deliver", 1), ("approve", "plan")),
       events=[fault_event(1), fault_event(2)], expect_attached=1, expect_wo_creates=1),
    _s("human_wo_exists", "Human already opened a WO -> reused, none created",
       Outcome.PLAN_VALID, _DELIVER_APPROVE, tweaks=Tweaks(human_wo_exists=True), expect_wo_creates=0),
    _s("unresolved_vehicle_id", "Telematics id not in crosswalk -> dead letter, no run",
       Outcome.DEAD_LETTER, _PROPOSE_ONLY, events=[fault_event(native="T-999")], expect_runs=0,
       expect_wo_creates=0),
    _s("product_down_on_read", "Scheduling times out during gather -> escalate degraded, never guess",
       Outcome.ESCALATE_DEGRADED, _PROPOSE_ONLY, chaos={"scheduling": "timeout"}, expect_wo_creates=0),
    _s("approval_rejected", "Dispatcher rejects the plan -> closed, zero writes",
       Outcome.REJECTED, (("deliver", 0), ("reject",)), expect_wo_creates=0),
    _s("approval_expired", "Nobody decides within the timeout -> escalate, zero writes",
       Outcome.EXPIRED_ESCALATED, (("deliver", 0), ("advance", 1)), expect_wo_creates=0),
    _s("write_rejected_mid_plan", "reassign_block rejected -> undo WO + unavailable in reverse, escalate",
       Outcome.COMPENSATED_ESCALATED, _DELIVER_APPROVE, chaos={"reject_write": "reassign_block"}),
    _s("kill_switch_mid_run", "Kill switch flips after 2 writes -> no further writes, escalate",
       Outcome.HALTED_KILL_SWITCH, _DELIVER_APPROVE, chaos={"kill_after_writes": 2}),
    _s("retried_write_duplicate", "Maintenance write delivered twice (retry) -> single WO effect",
       Outcome.PLAN_VALID, _DELIVER_APPROVE, chaos={"maintenance": "duplicate"}, expect_wo_creates=1),
    _s("spare_taken_after_proposal", "Spare grabbed after proposal -> re-propose v2, no write until re-approved",
       Outcome.PLAN_VALID, (("deliver", 0), ("take_spare",), ("approve", "plan"), ("approve", "plan")),
       tweaks=Tweaks(spares=3), expect_plan_version=2, expect_wo_creates=1),
)

BY_ID = {s.id: s for s in SCENARIOS}
