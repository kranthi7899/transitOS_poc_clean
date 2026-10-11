"""Phase 10 Step 2 scenarios: messy identifiers and the identity resolver.

Same baseline as `scenarios.py` (agency-a, veh-1 = scheduling BUS-44 / telematics T-77 / maintenance 1106, spares
veh-2.., OP-1 and OP-2). `Step2Tweaks` adds identity knobs; `vehicle_identity(n, tweaks)` gives the extra fleet keys
for veh-n, used by BOTH worlds (integration/fleets.py feeds the real mocks and crosswalk seed with them; the FakeWorld
builds its crosswalk rows from the same keys), so the two worlds cannot drift.

New outcome NEEDS_HUMAN_MATCH: no run, and the trigger is parked as AMBIGUOUS for a human. New script op
("pick", canonical_id): the human picks a candidate in the panel (POST /crosswalk), which republishes the parked
event(s).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from transitos.contracts import ids
from transitos.contracts.envelope import CloudEvent
from transitos.data.identifiers import make_vin, synthetic_wmis

from .scenarios import AGENCY, NATIVE_TELEMATICS, T0, Outcome, Scenario, _s
from .scenarios_step1 import Step1Tweaks

NEW_UNIT = "T-90"           # veh-1's new telematics gateway after the swap
NEW_ASSET = "M-1106"        # veh-1's maintenance asset number after the renumbering
OLD_ASSET = "1106"

_FULL = (("deliver", 0), ("approve", "plan"), ("advance", 72), ("close_wo",), ("approve", "return_to_service"))


@dataclass(frozen=True)
class Step2Tweaks(Step1Tweaks):
    identity: bool = False                  # scheduling records carry VIN, plate and name (crosswalk evidence)
    tele_swap_h: float | None = None        # veh-1's gateway T-77 is replaced by T-90 at T0 + h
    maint_renumber_h: float | None = None   # veh-1's asset 1106 is renumbered M-1106 at T0 + h
    maint_retired: bool = False             # veh-1's maintenance asset is marked retired (stale record)


def _iso(t: datetime) -> str:
    return t.isoformat().replace("+00:00", "Z")


def vehicle_identity(n: int, t: Any) -> dict[str, Any]:
    """Extra Fleet keys for baseline vehicle veh-n (n = 1 is the faulty bus BUS-44)."""
    g = lambda name, default=None: getattr(t, name, default)
    out: dict[str, Any] = {}
    if g("identity", False):
        out.update(sched_vin=make_vin(synthetic_wmis()[0], "B4D0F", 2022, "Z", 4400 + n),
                   sched_plate=f"ZZ {n}KTR{200 + n}", sched_name=f"Bus {43 + n}")
    if n != 1:
        return out
    if (h := g("tele_swap_h")) is not None:
        at = _iso(T0 + timedelta(hours=h))
        out.update(tele=NEW_UNIT, tele_installed_at=at, tele_prev={"id": NATIVE_TELEMATICS, "removed_at": at})
    if (h := g("maint_renumber_h")) is not None:
        out.update(maint=NEW_ASSET, maint_prev={"id": OLD_ASSET, "renumbered_at": _iso(T0 + timedelta(hours=h))})
    if g("maint_retired", False):
        out["maint_status"] = "retired"
    return out


def fault(native: str, minutes: float, spn: int = 201, **evidence: str) -> CloudEvent:
    at = T0 + timedelta(minutes=minutes)
    return CloudEvent(id=ids.fault_id(AGENCY, native, spn, 1, _iso(at)), source=f"adapter/telematics/{AGENCY}",
                      type="VehicleFaultDetected", time=at, agencyid=AGENCY,  # type: ignore[arg-type]
                      data={"native_vehicle_id": native, "spn": spn, "fmi": 1, "lamps": {"stop": True, "warn": True},
                            "occurrence_count": 3, **evidence})


def pm(native: str, hours: float, overdue_mi: int = 1500) -> CloudEvent:
    at = T0 + timedelta(hours=hours)
    return CloudEvent(id=ids.pm_id(AGENCY, native, "PM-A", 10000) + f":{int(hours)}",
                      source=f"adapter/maintenance/{AGENCY}", type="PMOverdue", time=at, agencyid=AGENCY,  # type: ignore[arg-type]
                      data={"native_asset_id": native, "service_code": "PM-A", "due_mi": 10000,
                            "meter_mi": 10000 + overdue_mi})


STEP2_SCENARIOS: tuple[Scenario, ...] = (
    _s("device_swap", "veh-1's gateway T-77 was swapped for T-90 a day ago: a fault from T-90 runs the plan; a late "
       "second delivery from the old unit, dated after the swap, is parked (never attributed to veh-1)",
       Outcome.PLAN_VALID,
       (("deliver", 0), ("approve", "plan"), ("deliver", 1), ("advance", 72), ("close_wo",),
        ("approve", "return_to_service")),
       events=[fault(NEW_UNIT, 1), fault(NATIVE_TELEMATICS, 2, spn=202)],
       tweaks=Step2Tweaks(tele_swap_h=-24.0), expect_wo_creates=1),
    _s("ambiguous_match", "Unknown gateway T-400 named 'Bus 4': equally close to Bus 44, 45 and 46 -> parked for a "
       "human with the candidates; no run, no write", Outcome.NEEDS_HUMAN_MATCH, (("deliver", 0),),
       events=[fault("T-400", 1, native_name="Bus 4")], tweaks=Step2Tweaks(identity=True), expect_runs=0,
       expect_wo_creates=0),
    _s("ambiguous_match_human_pick", "As ambiguous_match, then the human picks veh-1 in the panel: the parked event "
       "is republished, resolves, and the plan runs", Outcome.PLAN_VALID,
       (("deliver", 0), ("pick", "veh-1"), *_FULL[1:]),
       events=[fault("T-400", 1, native_name="Bus 4")], tweaks=Step2Tweaks(identity=True), expect_wo_creates=1),
    _s("renumbered_asset", "veh-1's maintenance asset 1106 was renumbered M-1106 a day ago: the work order goes to "
       "M-1106; a PM notice from 1106 dated before the renumbering still belongs to veh-1 (attached as context)",
       Outcome.PLAN_VALID,
       (("deliver", 0), ("approve", "plan"), ("deliver", 1), ("advance", 72), ("close_wo",),
        ("approve", "return_to_service")),
       events=[fault(NATIVE_TELEMATICS, 1), pm(OLD_ASSET, -48)], tweaks=Step2Tweaks(maint_renumber_h=-24.0),
       expect_wo_creates=1, expect_attached=1),
    _s("dirty_id_normalised", "Fault from 't-077 ' named 'bus 044' (dropped/extra zeros, case, spaces): resolves to "
       "veh-1; a second fault spelled 'T 77' / 'BUS-44 ' attaches to the same run, never a second vehicle",
       Outcome.PLAN_VALID,
       (("deliver", 0), ("approve", "plan"), ("deliver", 1), ("advance", 72), ("close_wo",),
        ("approve", "return_to_service")),
       events=[fault("t-077 ", 1, native_name="bus 044"), fault("T 77", 2, spn=203, native_name="BUS-44 ")],
       tweaks=Step2Tweaks(identity=True), expect_wo_creates=1, expect_attached=1),
    _s("orphan_telematics_unit", "A gateway with no maintenance asset and no roster entry (no VIN, unrelated name) "
       "reports a fault -> dead letter, no run, no write", Outcome.DEAD_LETTER, (("deliver", 0),),
       events=[fault("T-500", 1, native_name="Yard Truck 3")], tweaks=Step2Tweaks(identity=True), expect_runs=0,
       expect_wo_creates=0),
    _s("retired_asset_blocks_wo", "veh-1's maintenance asset is marked retired: the maintenance product refuses the "
       "work order (409) -> nothing else is written, escalate", Outcome.ESCALATE_NO_SAFE_OPTION,
       (("deliver", 0), ("approve", "plan")), tweaks=Step2Tweaks(maint_retired=True), expect_wo_creates=0),
)
