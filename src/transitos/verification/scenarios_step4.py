"""Phase 10 Step 4 scenarios: the maintenance shop's Pending Repair flow and zone-less local timestamps.

All four run a scripted day through the world engine on the scenario baseline (agency-a: veh-1 on blk-1, two
spares, OP-1 assigned, OP-2 on the extra board) against the REAL mock products. All four are `real_world_only`:
FakeWorld has no maintenance product, no foreman and no wire formats, so it SKIPS them with the reason instead of
agreeing with them. Script op ("expect_status", S) makes the report row fail when the run is not in status S at that
point of the day (Step 4 runner addition).

repair_with_parts_wait   PLAN_VALID. maintenance.mode pending_repair. veh-1 coolant fault 09:40 (stop lamp); approved
                         09:41: TransitOS asks the shop for a pending repair (no work order yet). The foreman puts it
                         on a work order at 09:51 (delay 10 min), starts it and it waits for parts for 3 h (parts
                         probability forced to 1 for the scenario). The shop rule (10:00) finds a record open and stands
                         down. The run must still be WAITING_FOR_REPAIR at 12:00 (parts wait) and at 13:30 (parts in,
                         repair going): WAITING_PARTS and IN_PROGRESS are not a close. The bus returns to service
                         14:00, the shop closes the work order 14:20, the run resumes and the return is approved.
human_wo_exists_pending  PLAN_VALID. maintenance.mode pending_repair. Approved 09:41 -> pending repair. A person (the shop
                         rule, A92: opened as human) opens a work order at 10:00, BEFORE the foreman (delay 30 min)
                         gets to the request at 10:11: the request joins that work order, no second work order exists.
                         The shop closes it after return to service (11:50) and the run resumes.
local_time_normalised    PLAN_VALID. The spec's coolant day with the local-time knob ON: maintenance and the workforce
                         CSV send zone-less America/New_York wall time (with per-field utc_offset), telematics sends
                         epoch milliseconds. Same outcome and writes as scripted_day_coolant.
dst_fallback_event       PLAN_VALID. Same knob, on the 2026 US fall-back day (2026-11-01, 02:00 EDT -> 01:00 EST); the
                         sim clock starts 05:00Z (01:00 EDT) via the `clock_start` knob, so blk-1 runs 06:00Z-10:00Z.
                         Fault 05:40Z = 01:40 EDT; work order opened 05:41Z ("01:41"); return to service 06:10Z; the
                         shop closes at 06:30Z = "01:30" EST, a local wall time EARLIER than the opening. The close must
                         still be seen (the poll cursor never skips it) and stamped 06:30Z (the product's utc_offset
                         disambiguates). Fix round F2: the operators' availability starts 05:00Z, which the offset-less
                         workforce CSV says as "01:00" (ambiguous); it is read conservatively as 06:00Z (A145), which
                         is not after the 06:00Z block start. With the block at 05:00Z (the first draft, clock_start
                         04:00Z) nobody is known to be available and the run escalates: that case is a test
                         (test_through_clearance_on_the_fall_back_day_an_ambiguous_start_is_not_available).

Known gap (A112): without the product's utc_offset, the second 01:30 is read as the first (documented rule), so a
close or event time in the repeated hour is stamped an hour early. That affects only the event's own time, not
clearance (the workforce CSV, which has no offsets, is read conservatively per field, A145). dst_fallback_event runs
WITH offsets; a test pins the without-offset consequence.

Why a `clock_start` knob (A114): every older scenario starts at T0 = 2026-10-10 06:00Z and IntegrationWorld built its
SimClock from that constant; there was no other mechanism. Default None keeps T0 for every older scenario.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from transitos.world.script import Beat, DayScript

from .scenarios import AGENCY, T0, Outcome, Scenario, _s, fault_event
from .scenarios_step3 import Step3Tweaks, coolant_day

NY = "America/New_York"
FALL_BACK = datetime(2026, 11, 1, tzinfo=UTC)       # 2026 US fall-back day
SPRING_FORWARD = datetime(2026, 3, 8, tzinfo=UTC)   # 2026 US spring-forward day (unit and e2e tests)
WIRE_REASON = ("needs the real products' wire formats (zone-less local time, epoch ms) and the maintenance "
               "product's pending-repair flow with the foreman; FakeWorld has neither")


@dataclass(frozen=True)
class ForemanSpec:
    delay_min: float | None = None
    parts_wait_prob: float | None = None
    defer_prob: float | None = None
    parts_hours: float | None = None
    defer_hours: float | None = None
    seed: int = 0


@dataclass(frozen=True)
class Step4Tweaks(Step3Tweaks):
    clock_start: datetime | None = None        # sim clock start (default T0); also the scenario day for world_until
    maintenance_mode: str = "work_order"       # registry flag maintenance.mode (A105); pending_repair = Step 4.1
    foreman: ForemanSpec | None = None         # a ForemanActor joins the world engine (A106)
    local_time: bool = False                   # products send zone-less local time / epoch ms (A108, A110)
    utc_offset: bool = True                    # with local_time: the maintenance product also sends utc_offset


def _at(day: datetime, h: int, m: int = 0) -> datetime:
    return day.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(hours=h, minutes=m)


def _coolant(day: datetime, on: tuple[int, int], off: tuple[int, int], back: tuple[int, int]) -> DayScript:
    fault = {"spn": 110, "fmi": 0, "lamps": {"stop": True, "warn": True}}
    return DayScript(AGENCY, (
        Beat(_at(day, *on), "veh-1", "fault_on", fault),
        Beat(_at(day, *off), "veh-1", "fault_off", {"spn": 110, "fmi": 0}),
        Beat(_at(day, *back), "veh-1", "return_to_service", {}),
    ))


def _tw(**kw) -> Step4Tweaks:
    return dataclasses.replace(Step4Tweaks(), **kw)


_RETURN = (("approve", "return_to_service"),)

GAP_LOCAL_WIRE = ("A108: this row's checks (outcome, writes, UTC event times) would pass just the same if the knob "
                  "were ignored and every product sent UTC; it does NOT prove the wire was zone-less local time or "
                  "epoch ms. That proof is test_local_time_wire_is_really_local_and_the_bus_is_utc.")
GAP_FALLBACK = ("A112: passes because the maintenance product sends a per-field utc_offset. Without one, the second "
                "01:30 is read as the first (documented rule) and the close event is stamped 05:30Z, an hour early "
                "and before its own opening (pinned by test_dst_fallback_without_offsets_follows_the_documented_rule"
                "_known_gap). Only the event's own time is wrong; clearance reads the offset-less CSV conservatively "
                "(A145).")

STEP4_SCENARIOS: tuple[Scenario, ...] = (
    _s("repair_with_parts_wait", "Pending repair: the foreman puts it on a work order at 09:51, parts wait 3 h; the "
       "run stays in wait_for_repair through WAITING_PARTS and IN_PROGRESS and resumes only on the 14:20 close",
       Outcome.PLAN_VALID,
       (("world_until", "09:41"), ("approve", "plan"), ("world_until", "12:00"), ("expect_status", "WAITING_FOR_REPAIR"),
        ("world_until", "13:30"), ("expect_status", "WAITING_FOR_REPAIR"), ("world_until", "15:00"), *_RETURN),
       events=[fault_event()],
       tweaks=_tw(world_script=_coolant(T0, (9, 40), (10, 15), (14, 0)), maintenance_mode="pending_repair",
                  foreman=ForemanSpec(delay_min=10, parts_wait_prob=1.0, defer_prob=0.0, parts_hours=3.0)),
       expect_wo_creates=1, expect_work_orders=1, expect_pending_repairs=1,
       expect_event_times=(("WorkOrderOpened", "2026-10-10T09:51:00+00:00"),       # the foreman's conversion
                           ("WorkOrderClosed", "2026-10-10T14:20:00+00:00")),      # the shop's close, not earlier
       real_world_only=True, real_world_reason=WIRE_REASON),
    _s("human_wo_exists_pending", "Pending repair at 09:41; a person opens a work order at 10:00 before the foreman "
       "reaches the request (10:11): the request joins it, no second work order",
       Outcome.PLAN_VALID,
       (("world_until", "09:41"), ("approve", "plan"), ("world_until", "10:30"), ("expect_status", "WAITING_FOR_REPAIR"),
        ("world_until", "12:00"), *_RETURN),
       events=[fault_event()],
       tweaks=_tw(world_script=coolant_day(), maintenance_mode="pending_repair",
                  foreman=ForemanSpec(delay_min=30, parts_wait_prob=0.0, defer_prob=0.0)),
       expect_wo_creates=1, expect_work_orders=1, expect_pending_repairs=1,
       expect_event_times=(("WorkOrderOpened", "2026-10-10T10:00:00+00:00"),       # the person's work order
                           ("WorkOrderClosed", "2026-10-10T11:50:00+00:00")),
       real_world_only=True, real_world_reason=WIRE_REASON),
    _s("local_time_normalised", "Coolant day with zone-less local time from maintenance and the workforce CSV and "
       "epoch-ms telematics: normalised to UTC, same outcome as scripted_day_coolant",
       Outcome.PLAN_VALID, (("world_until", "09:41"), ("approve", "plan"), ("world_until", "12:00"), *_RETURN),
       events=[fault_event()], tweaks=_tw(world_script=coolant_day(), local_time=True),
       expect_wo_creates=1, expect_work_orders=1, expect_pending_repairs=0,
       expect_event_times=(("VehicleFaultDetected", "2026-10-10T09:40:05+00:00"),  # epoch ms on the wire
                           ("WorkOrderOpened", "2026-10-10T09:41:00+00:00"),       # "05:41" local on the wire
                           ("WorkOrderClosed", "2026-10-10T11:50:00+00:00")),
       known_gap=GAP_LOCAL_WIRE, real_world_only=True, real_world_reason=WIRE_REASON),
    _s("dst_fallback_event", "Fall-back day 2026-11-01: work order opened 01:41 EDT, closed 01:30 EST (an earlier "
       "local wall time); the close is seen, stamped 06:30Z, and the run returns to service",
       Outcome.PLAN_VALID,
       (("world_until", "05:41"), ("approve", "plan"), ("world_until", "06:20"), ("expect_status", "WAITING_FOR_REPAIR"),
        ("world_until", "07:00"), *_RETURN),
       events=[fault_event()],
       tweaks=_tw(world_script=_coolant(FALL_BACK, (5, 40), (6, 0), (6, 10)), local_time=True,
                  clock_start=_at(FALL_BACK, 5)),
       expect_wo_creates=1, expect_work_orders=1, expect_pending_repairs=0,
       expect_event_times=(("WorkOrderOpened", "2026-11-01T05:41:00+00:00"),       # "01:41" EDT, first pass
                           ("WorkOrderClosed", "2026-11-01T06:30:00+00:00")),      # "01:30" EST, second pass
       known_gap=GAP_FALLBACK, real_world_only=True, real_world_reason=WIRE_REASON),
)
