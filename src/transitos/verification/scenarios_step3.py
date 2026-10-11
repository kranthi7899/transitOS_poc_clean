"""Phase 10 Step 3 scenarios: the faults come from a scripted DAY (ground truth) run by the world engine, not from
hand-fired events. The real mock products see each beat after their own lag; the orchestrator reacts to what the
products show.

Baseline fleet is the scenario baseline (agency-a): veh-1 on blk-1 (07:00-11:00 on the scenario day, T0 = 06:00),
two ready spares, OP-1 assigned, OP-2 on the extra board. New script op: ("world_until", "HH:MM") runs the world
engine to that time of day. No scenario here reads the wall clock or the network.

scripted_day_coolant        PLAN_VALID. The spec's example day on veh-1: coolant fault (SPN 110) 09:40, clears 10:15,
                            return to service 11:30. Telematics shows it at 09:40:05; the run proposes; the dispatcher
                            approves at 09:41 (the pull lands then). The dispatcher (09:52) and the shop rule (10:00)
                            find the work done and stand down. The shop closes the work order at 11:50 and the return
                            to service is approved.
intermittent_fault_clears   PLAN_VALID with the recurrence attached, one run, one work order. A battery-voltage warning
                            (SPN 168) shows at 08:00, clears 08:06 and recurs 08:10. The orchestrator has no
                            fault-cleared signal (the telematics mock emits no event when a fault clears: A101) and
                            never plans MONITOR for a fault event (the stub and the fallback always pull; only a minor
                            PM overdue is MONITOR), so the first occurrence gets a plan, the recurrence is attached to
                            that run (A9) and the plan is approved at 08:12. MONITOR was the other candidate outcome;
                            it is ruled out by how the orchestrator behaves today, and the scenario pins that down (A100).
callout_with_extra_board    PLAN_VALID. OP-1 calls out at 06:10; the dispatcher records it in workforce at 06:22 (12 min
                            lag). veh-1 faults at 06:40, after the record exists, so the run sees OP-1 unavailable and
                            swaps in OP-2 from the extra board (a fifth saga step, assign_operator).
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import timedelta

from transitos.world.lag import ProductLag
from transitos.world.script import Beat, DayScript

from .scenarios import AGENCY, T0, Outcome, Scenario, Tweaks, _s, fault_event

_FULL_DAY = (("world_until", "12:00"), ("approve", "return_to_service"))


@dataclass(frozen=True)
class Step3Tweaks(Tweaks):
    world_script: DayScript | None = None     # the day to run; the integrated world starts a world engine for it
    world_lag: ProductLag | None = None       # default ProductLag() (ASSUMED A91)


def _at(h: int, m: int = 0):
    return T0.replace(hour=0, minute=0) + timedelta(hours=h, minutes=m)


def coolant_day() -> DayScript:
    """The spec's example day, on this baseline's faulty vehicle."""
    return DayScript.example_coolant_fault(T0, vehicle="veh-1", agency=AGENCY)


def intermittent_day() -> DayScript:
    f = {"fault_id": "veh-1-f1", "spn": 168, "fmi": 1}           # battery voltage far below range: service_soon
    on = {**f, "lamps": {"stop": False, "warn": True}, "withdrawn": False}
    return DayScript(AGENCY, (
        Beat(_at(8, 0), "veh-1", "fault_on", on),
        Beat(_at(8, 6), "veh-1", "fault_off", f),
        Beat(_at(8, 10), "veh-1", "fault_on", on),                # recurs
        Beat(_at(8, 25), "veh-1", "fault_off", f),
        Beat(_at(9, 30), "veh-1", "return_to_service", {}),       # inspection signs it off
    ))


def callout_day(fault_min: int = 40) -> DayScript:
    """OP-1 calls out at 06:10 (recorded at 06:22); veh-1 faults `fault_min` minutes after 06:00."""
    return DayScript(AGENCY, (
        Beat(_at(6, 10), "op-1", "callout", {"operator": "op-1"}),
        Beat(_at(6, fault_min), "veh-1", "fault_on", {"spn": 110, "fmi": 0, "lamps": {"stop": True, "warn": True}}),
        Beat(_at(10, 15), "veh-1", "fault_off", {"spn": 110, "fmi": 0}),
        Beat(_at(11, 30), "veh-1", "return_to_service", {}),
    ))


def _tw(day: DayScript) -> Step3Tweaks:
    return dataclasses.replace(Step3Tweaks(), world_script=day)


STEP3_SCENARIOS: tuple[Scenario, ...] = (
    _s("scripted_day_coolant", "Scripted day: coolant fault 09:40, clears 10:15, returns 11:30; products learn it "
       "after their own lag; the run pulls the vehicle at 09:41",
       Outcome.PLAN_VALID, (("world_until", "09:41"), ("approve", "plan"), *_FULL_DAY),
       events=[fault_event()], tweaks=_tw(coolant_day()), expect_wo_creates=1),
    _s("intermittent_fault_clears", "Intermittent warning clears at 08:06 and recurs at 08:10: one run, the "
       "recurrence attached (no clear signal reaches the orchestrator), one work order",
       Outcome.PLAN_VALID, (("world_until", "08:12"), ("approve", "plan"), *_FULL_DAY),
       events=[fault_event(1, stop=False), fault_event(2, stop=False)], tweaks=_tw(intermittent_day()), expect_wo_creates=1, expect_attached=1),
    _s("callout_with_extra_board", "OP-1 calls out (recorded 12 min later), then veh-1 faults: the plan swaps in "
       "the extra-board operator OP-2",
       Outcome.PLAN_VALID, (("world_until", "06:41"), ("approve", "plan"), *_FULL_DAY),
       events=[fault_event()], tweaks=_tw(callout_day()), expect_wo_creates=1),
)
