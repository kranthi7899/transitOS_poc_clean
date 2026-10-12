"""The people and devices that turn ground truth into what each product holds (Phase 10, Step 3.2).

Each actor owns ONE product and ONE lag. `handles(beat)` says whether the beat concerns it at all; `act` returns
None when it did its job or a reason string when a rule made it stand down (a skip is logged by the engine, it is
not an observation). Severity comes from the committed J1939 fixture's configurable rule (never ground truth).

  TelematicsActor  fault_on / fault_off / dvir  -> telematics, after telematics_s
  ShopActor        fault_on / dvir -> opens a maintenance record, return_to_service -> closes it, after
                   shop_rule_min (A91). It stands down when a record is already open for the vehicle (a rule
                   de-duplicates). Records are opened as `human` (A92: a foreman or controller keys it, so the
                   orchestrator reuses it instead of opening a second one).
  ForemanActor     (Phase 10 Step 4.1) watches maintenance for open pending repairs and puts each on a work order
                   after a delay; sometimes a parts wait, sometimes a deferral back to pending (A106)
  DispatcherActor  fault_on with the stop lamp -> sets scheduling out_of_service, call-out -> records the operator
                   as unavailable in workforce, after dispatcher_min. It stands down when scheduling already
                   shows the vehicle out of service.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import datetime, timedelta

from transitos.data.loaders import load_j1939, load_shop_behaviour

from .lag import ProductLag
from .ports import Ports
from .script import Beat

_SEVERITY_OPENS_RECORD = ("service_soon", "pull_vehicle")

# Bug sweep B fix round (A168, A169): what `act` returns when the actor did nothing worth a row: neither an
# observation nor a skip (a return it is waiting on, or one that is not its business).
QUIET = "__quiet__"


class Actor:
    product: str = ""
    kinds: tuple[str, ...] = ()

    def __init__(self, lag: ProductLag) -> None:
        self.lag = lag

    def product_of(self, beat: Beat) -> str:
        """The product whose view this beat changes (DispatcherActor serves two)."""
        return self.product

    def delay(self, beat: Beat) -> timedelta:
        return self.lag.delay(self.product_of(beat))

    def handles(self, beat: Beat) -> bool:
        return beat.kind in self.kinds

    def act(self, beat: Beat, ports: Ports) -> str | None:   # pragma: no cover - interface
        raise NotImplementedError


class TelematicsActor(Actor):
    product = "telematics"
    kinds = ("fault_on", "fault_off", "dvir")

    def handles(self, beat: Beat) -> bool:
        # a fault without an SPN is not a J1939 fault: telematics cannot show it (a driver reports it by DVIR)
        if beat.kind in ("fault_on", "fault_off"):
            return beat.params.get("spn") is not None and beat.params.get("fmi") is not None
        return beat.kind == "dvir"

    def act(self, beat: Beat, ports: Ports) -> str | None:
        {"fault_on": ports.telematics_fault, "fault_off": ports.telematics_clear,
         "dvir": ports.telematics_dvir}[beat.kind](beat.vehicle, dict(beat.params))
        return None


class ShopActor(Actor):
    product = "maintenance"
    kinds = ("fault_on", "dvir", "return_to_service")

    def __init__(self, lag: ProductLag) -> None:
        super().__init__(lag)
        self._j1939 = load_j1939()

    def handles(self, beat: Beat) -> bool:
        if beat.kind == "fault_on":
            return self._opens_record(beat)
        return beat.kind in ("dvir", "return_to_service")

    def _opens_record(self, beat: Beat) -> bool:
        p = beat.params
        if (p.get("lamps") or {}).get("stop"):
            return True
        if p.get("spn") is None or p.get("fmi") is None:
            return False
        return self._j1939.severity(int(p["spn"]), int(p["fmi"])) in _SEVERITY_OPENS_RECORD

    def act(self, beat: Beat, ports: Ports) -> str | None:
        if beat.kind == "return_to_service":
            ports.maintenance_close(beat.vehicle)
            return None
        if ports.maintenance_open_ids(beat.vehicle):
            return "a record is already open for the vehicle"
        what = beat.params.get("defect_text") or f"SPN {beat.params.get('spn')} FMI {beat.params.get('fmi')}"
        ports.maintenance_open(beat.vehicle, f"shop rule: {what}")
        return None


@dataclass(frozen=True)
class ForemanPlan:
    """What the foreman will do with one pending repair, drawn once from its own seeded stream (A103 pattern)."""
    delay: timedelta                 # pending repair -> on a work order
    parts: timedelta | None          # WAITING_PARTS for this long, or no parts wait
    defer: timedelta | None          # deferred back to PENDING for this long, or no deferral


@dataclass(frozen=True)
class ForemanJob:
    kind: str                        # "convert" | "status"
    pr_id: str
    vehicle: str
    wo_id: str | None = None
    status: str | None = None


class ForemanActor(Actor):
    """Phase 10 Step 4.1 (A106): the shop foreman. Not driven by truth beats: it WATCHES the maintenance product for
    open pending repairs (a request TransitOS or anyone else made), and after `delay_min` (or a lognormal draw from
    config/shop_behaviour.yaml) puts each on a work order. If a work order is already open for the asset (a person
    opened one first) the request joins it and the foreman leaves that work order to its owner. A work order it
    creates goes IN_PROGRESS; with probability `parts_wait_prob` it then waits for parts (WAITING_PARTS, then back
    IN_PROGRESS), and with probability `defer_prob` the repair is deferred back to PENDING for a while. The foreman
    never closes a work order: the shop closes it when the bus returns to service (ShopActor, A92).

    Numbers: everything not passed comes from config/shop_behaviour.yaml (ASSUMED, A119: pending->WO median 4 h,
    parts wait 25% for median 36 h, deferral 10% for median 24 h). data/derived/repair_durations.yaml is NOT used: it
    has whole-day resolution and no pending->work-order delay (A111). A request is first seen when the engine next
    looks (the start of a run_until step), so the delay runs from then (never before the request exists)."""

    product = "maintenance"
    kinds = ()
    source = "config/shop_behaviour.yaml (ASSUMED, A119)"

    def __init__(self, delay_min: float | None = None, parts_wait_prob: float | None = None, seed: int | str = 0,
                 *, defer_prob: float | None = None, parts_hours: float | None = None,
                 defer_hours: float | None = None, lag: ProductLag | None = None) -> None:
        super().__init__(lag or ProductLag())
        sb = load_shop_behaviour()
        self.behaviour = sb
        self.parts_wait_prob = sb.parts_wait.probability if parts_wait_prob is None else float(parts_wait_prob)
        self.defer_prob = sb.deferral_to_pending.probability if defer_prob is None else float(defer_prob)
        for name, p in (("parts_wait_prob", self.parts_wait_prob), ("defer_prob", self.defer_prob)):
            if not 0.0 <= p <= 1.0:
                raise ValueError(f"{name} must be in [0, 1], got {p}")
        for name, v in (("delay_min", delay_min), ("parts_hours", parts_hours), ("defer_hours", defer_hours)):
            if v is not None and v < 0:
                raise ValueError(f"{name} must be >= 0, got {v}")
        self.delay_min, self.parts_hours, self.defer_hours = delay_min, parts_hours, defer_hours
        self.seed = seed
        self._seen: set[str] = set()
        self._queued: list[tuple[datetime, ForemanJob]] = []

    def handles(self, beat: Beat) -> bool:
        return False

    def plan(self, pr_id: str) -> ForemanPlan:
        """Seeded per request: `random.Random("<seed>|foreman|<pr id>")`, draws always in the same order."""
        rng = random.Random(f"{self.seed}|foreman|{pr_id}")
        u_parts, u_defer = rng.random(), rng.random()
        sb = self.behaviour
        draw = {k: rng.lognormvariate(math.log(d.median), d.sigma)
                for k, d in (("delay", sb.pending_to_work_order_hours), ("parts", sb.parts_wait.hours),
                             ("defer", sb.deferral_to_pending.hours))}
        delay = timedelta(minutes=self.delay_min) if self.delay_min is not None else timedelta(hours=draw["delay"])
        parts = timedelta(hours=self.parts_hours if self.parts_hours is not None else draw["parts"])
        defer = timedelta(hours=self.defer_hours if self.defer_hours is not None else draw["defer"])
        return ForemanPlan(delay, parts if u_parts < self.parts_wait_prob else None,
                           defer if u_defer < self.defer_prob else None)

    def discover(self, now: datetime, ports: Ports) -> list[tuple[datetime, ForemanJob]]:
        """New open pending repairs (scheduled at now + delay) and follow-ups queued by `perform`."""
        out = []
        for pr in ports.maintenance_pending():
            if pr["id"] in self._seen:
                continue
            self._seen.add(pr["id"])
            out.append((now + self.plan(pr["id"]).delay, ForemanJob("convert", pr["id"], pr["vehicle"])))
        out, self._queued = out + self._queued, []
        return out

    def perform(self, job: ForemanJob, ports: Ports, now: datetime) -> str | None:
        if job.kind == "convert":
            res = ports.maintenance_convert(job.pr_id)
            if res is None:
                return "the request was cancelled before it reached a work order"
            if not res["created"]:
                return f"joined work order {res['work_order']} that someone else opened: left to them"
            wo, p, t = res["work_order"], self.plan(job.pr_id), now
            steps = [(t, "IN_PROGRESS")]
            if p.parts is not None:
                steps += [(t, "WAITING_PARTS"), (t + p.parts, "IN_PROGRESS")]
                t = t + p.parts
            if p.defer is not None:
                steps += [(t, "PENDING"), (t + p.defer, "IN_PROGRESS")]
            self._queued += [(at, ForemanJob("status", job.pr_id, job.vehicle, wo, s)) for at, s in steps]
            return None
        assert job.wo_id is not None and job.status is not None
        if str(ports.maintenance_wo_status(job.wo_id)).lower() == "closed":
            return f"work order {job.wo_id} is already closed"
        ports.maintenance_set_status(job.wo_id, job.status)
        return None


class DispatcherActor(Actor):
    """Bug sweep B (F13-R3, A156): on `return_to_service` the dispatcher puts back a vehicle IT pulled, after the same
    dispatcher lag (12 min, ASSUMED A91), to the status it had before the pull (in_service for a bus in service).
    Only when scheduling still shows out_of_service because of this world's pull: the vehicle is in this actor's own
    pull record AND the latest scheduling change is the simulated dispatcher's. If the orchestrator or a person
    changed it since, the return is theirs (A92) and the dispatcher stands down.

    Fix round B1 (A168): and only when maintenance holds NO open work order or pending repair for the vehicle. While
    one is open the dispatcher waits (nothing is logged) and re-checks at every engine step (`discover`); the moment
    it sees the record closed it acts on the same return_to_service beat, so the bus goes back at the close (never
    earlier, and never before return_to_service + its own lag). Fix round B2 (A169): a return_to_service for a bus
    this dispatcher never pulled is not its business and leaves no row at all."""

    product = "scheduling"
    kinds = ("fault_on", "callout", "return_to_service")

    def __init__(self, lag: ProductLag) -> None:
        super().__init__(lag)
        self._pulled: dict[str, str] = {}     # vehicle -> scheduling status before this world pulled it
        self._waiting: dict[str, Beat] = {}   # B1: return_to_service beats waiting for maintenance to close

    def discover(self, now: datetime, ports: Ports) -> list[tuple[datetime, Beat]]:
        """B1: a waiting return is due again NOW once maintenance shows nothing open for the vehicle."""
        out = []
        for vehicle, beat in sorted(self._waiting.items()):
            if not self._maintenance_open(vehicle, ports):
                del self._waiting[vehicle]
                out.append((now, beat))
        return out

    @staticmethod
    def _maintenance_open(vehicle: str, ports: Ports) -> bool:
        return bool(ports.maintenance_open_ids(vehicle)) or any(
            p.get("vehicle") == vehicle for p in ports.maintenance_pending())

    def handles(self, beat: Beat) -> bool:
        if beat.kind == "fault_on":
            return bool((beat.params.get("lamps") or {}).get("stop"))
        return beat.kind in ("callout", "return_to_service")

    def product_of(self, beat: Beat) -> str:
        return "workforce" if beat.kind == "callout" else "scheduling"

    def act(self, beat: Beat, ports: Ports) -> str | None:
        if beat.kind == "callout":
            ports.workforce_callout(beat.vehicle, dict(beat.params))
            return None
        if beat.kind == "return_to_service":
            return self._return(beat, ports)
        before = ports.scheduling_status(beat.vehicle)
        if before == "out_of_service":
            return "scheduling already shows the vehicle out of service"
        ports.scheduling_out_of_service(beat.vehicle)
        self._pulled[beat.vehicle] = before
        return None

    def _return(self, beat: Beat, ports: Ports) -> str | None:
        vehicle = beat.vehicle
        if vehicle not in self._pulled:
            return QUIET                      # B2 (A169): never pulled by this actor: no row
        if self._maintenance_open(vehicle, ports):
            self._waiting[vehicle] = beat     # B1 (A168): wait for the shop to close the record
            return QUIET
        before = self._pulled.pop(vehicle)
        if ports.scheduling_status(vehicle) != "out_of_service":
            return "scheduling no longer shows the vehicle out of service"
        if not ports.scheduling_changed_by_world(vehicle):
            return "someone else changed the vehicle's status since the dispatcher pulled it: their return"
        ports.scheduling_set_status(vehicle, before)
        return None
