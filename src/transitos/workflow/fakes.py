"""In-memory ReadTools/WriteTools with fault injection, for tests and for other tracks' dry runs.
Not a mock product: it implements the contract protocols directly. Writes are idempotent by key."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from transitos.clearance.facts import native_profile
from transitos.contracts.envelope import CloudEvent
from transitos.contracts.objects import Block, Needs, Operator, Vehicle, WorkOrder
from transitos.contracts.policy import AgencyPolicy
from transitos.contracts.results import Result

AGENCY = "agency-a"
T0 = datetime(2026, 1, 5, 8, 0, tzinfo=UTC)


class SimulatedCrash(BaseException):
    """Simulates the process being killed (not caught by `except Exception`)."""


class FakeClock:
    def __init__(self, start: datetime = T0):
        self.t = start

    def now(self) -> datetime:
        return self.t

    __call__ = now

    def advance(self, hours: float = 0, minutes: float = 0) -> None:
        self.t += timedelta(hours=hours, minutes=minutes)


@dataclass
class FakeWorld:
    vehicles: dict[str, Vehicle] = field(default_factory=dict)
    blocks: dict[str, Block] = field(default_factory=dict)
    operators: dict[str, Operator] = field(default_factory=dict)
    work_orders: dict[str, WorkOrder] = field(default_factory=dict)
    alerts: list[dict] = field(default_factory=list)
    # fault injection
    fail_writes: set[str] = field(default_factory=set)          # action names that always fail
    fail_on_block: dict[str, set[str]] = field(default_factory=dict)  # action -> block ids that fail
    fail_compensations: set[str] = field(default_factory=set)   # "action" or "action:block" keys
    crash_before: str | None = None                             # action name -> raise SimulatedCrash once
    lose_response_once: set[str] = field(default_factory=set)   # apply, then report unavailable (retry same key)
    unavailable_reads: set[str] = field(default_factory=set)
    # bookkeeping
    calls: list[tuple[str, str]] = field(default_factory=list)       # (name, key) every attempt
    effects: list[tuple[str, str]] = field(default_factory=list)     # (name, key) applied exactly once
    _by_key: dict[str, Result] = field(default_factory=dict)
    _wo_seq: int = 0

    def take_spare(self, vehicle_id: str) -> None:
        """Another dispatcher grabs the spare."""
        self.vehicles[vehicle_id].status = "in_service"

    def effect_names(self) -> list[str]:
        return [n for n, _ in self.effects]


def default_world(now: datetime = T0) -> FakeWorld:
    w = FakeWorld()
    w.vehicles = {
        "veh-1": Vehicle(id="veh-1", agency=AGENCY, seats=40, status="in_service", depot="north"),
        "veh-2": Vehicle(id="veh-2", agency=AGENCY, seats=40, status="ready", depot="north"),
        "veh-3": Vehicle(id="veh-3", agency=AGENCY, seats=40, status="ready", depot="south"),
        "veh-4": Vehicle(id="veh-4", agency=AGENCY, seats=40, status="ready", depot="south"),
    }
    w.blocks = {"B1": Block(id="B1", vehicle_id="veh-1", operator_id="OP-old", start=now + timedelta(hours=1),
                            end=now + timedelta(hours=5), start_depot="north", needs=Needs(seats=20, type="bus"))}
    med = now + timedelta(days=365)   # F8: medical card is a legal rule; every fake operator holds a valid one
    w.operators = {
        "OP-old": Operator(id="OP-old", qualifications=["bus"], hours_today=9.5, medical_valid_to=med),  # > 10h
        "OP-1": Operator(id="OP-1", qualifications=["bus"], hours_today=2.0, medical_valid_to=med),
        "OP-2": Operator(id="OP-2", qualifications=["bus"], hours_today=4.0, medical_valid_to=med),
        "OP-unq": Operator(id="OP-unq", qualifications=["van"], hours_today=0.0, medical_valid_to=med),  # not qualified
    }
    return w


class FakeReadTools:
    def __init__(self, world: FakeWorld):
        self.w = world

    def _u(self, name: str):
        return name in self.w.unavailable_reads

    def get_vehicle(self, agency, vehicle_id):
        if self._u("get_vehicle"):
            return Result.unavailable("scheduling", "down")
        return Result.success(self.w.vehicles[vehicle_id].model_copy())

    def get_vehicle_history(self, agency, vehicle_id):
        return Result.success([])

    def list_affected_blocks(self, agency, vehicle_id, start, end):
        if self._u("list_affected_blocks"):
            return Result.unavailable("scheduling", "down")
        return Result.success([b.model_copy() for b in self.w.blocks.values()
                               if b.vehicle_id == vehicle_id and b.end > start and b.start < end])

    def list_ready_vehicles(self, agency, start, end, needs: Needs):
        if self._u("list_ready_vehicles"):
            return Result.unavailable("scheduling", "down")
        return Result.success([v.model_copy() for v in self.w.vehicles.values() if v.status == "ready"
                               and v.seats >= needs.seats and v.wheelchair_positions >= needs.wheelchair_positions
                               and (not needs.lift or v.lift_ok)])

    def list_eligible_operators(self, agency, block):
        if self._u("list_eligible_operators"):
            return Result.unavailable("workforce", "down")
        return Result.success([o.model_copy() for o in self.w.operators.values()])  # code applies hard limits

    def capability_profiles(self, agency):
        """The fake supplies the legacy operator fields (no licence/endorsement/training columns) plus the
        medical card date (a legal rule since Step 1b, F8)."""
        drop = ("licence_class", "endorsements", "trained_vehicle_types", "training_valid_to", "duty_times")
        return Result.success({"scheduling": native_profile("scheduling").to_dict(),
                               "workforce": native_profile("workforce", drop=drop).to_dict()})

    def list_blocks(self, agency, start, end):
        return Result.success([b.model_copy() for b in self.w.blocks.values() if b.end > start and b.start < end])

    def find_open_work_order(self, agency, vehicle_id):
        for wo in self.w.work_orders.values():
            if wo.asset_vehicle_id == vehicle_id and wo.status != "closed":
                return Result.success(wo.model_copy())
        return Result.success(None)

    def get_work_order(self, agency, wo_id):
        return Result.success(self.w.work_orders[wo_id].model_copy())


class FakeWriteTools:
    def __init__(self, world: FakeWorld):
        self.w = world

    def _run(self, name: str, key: str, block: str | None, fn, comp: bool = False) -> Result:
        w = self.w
        w.calls.append((name, key))
        if w.crash_before == name and not comp:
            w.crash_before = None
            raise SimulatedCrash(name)
        if key in w._by_key:  # idempotent replay: same key -> same single effect
            return w._by_key[key]
        fails = w.fail_compensations if comp else w.fail_writes
        if name in fails or (block and f"{name}:{block}" in fails):
            return Result.unavailable("fake", f"injected failure {name}")
        if not comp and block and block in w.fail_on_block.get(name, set()):
            return Result.unavailable("fake", f"injected failure {name}:{block}")
        res = fn()
        if res.status == "ok":
            w._by_key[key] = res
            w.effects.append((name, key))
        if name in w.lose_response_once and not comp:
            w.lose_response_once.discard(name)
            return Result.unavailable("fake", "lost response")
        return res

    def create_work_order(self, agency, vehicle_id, description, key):
        def f():
            self.w._wo_seq += 1
            wo = WorkOrder(id=f"WO-{self.w._wo_seq}", native_no=str(1000 + self.w._wo_seq),
                           asset_vehicle_id=vehicle_id, description=description, source="system")
            self.w.work_orders[wo.id] = wo
            return Result.success(wo)
        return self._run("create_work_order", key, None, f)

    def cancel_work_order(self, agency, wo_id, key):
        def f():
            wo = self.w.work_orders[wo_id]
            wo.status = "closed"
            return Result.success(wo)
        return self._run("cancel_work_order", key, None, f, comp=True)

    def set_vehicle_unavailable(self, agency, vehicle_id, start, end, key):
        def f():
            self.w.vehicles[vehicle_id].status = "out_of_service"
            return Result.success(self.w.vehicles[vehicle_id])
        return self._run("set_vehicle_unavailable", key, None, f)

    def set_vehicle_available(self, agency, vehicle_id, key):
        def f():
            self.w.vehicles[vehicle_id].status = "ready"
            return Result.success(self.w.vehicles[vehicle_id])
        return self._run("set_vehicle_available", key, None, f, comp="comp:" in key)

    def reassign_block(self, agency, block_id, to_vehicle_id, key, *, compensation=False):
        comp = compensation or "comp:" in key

        def f():
            b = self.w.blocks[block_id]
            to = self.w.vehicles[to_vehicle_id]
            if not comp and to.status != "ready":
                return Result.unavailable("fake", "spare taken")
            if not comp and any(o.vehicle_id == to_vehicle_id and o.id != block_id and o.start < b.end
                                and o.end > b.start for o in self.w.blocks.values()):
                return Result.unavailable("fake", "spare double-booked")   # a spare may serve non-overlapping blocks
            b.vehicle_id = to_vehicle_id
            return Result.success(b)
        return self._run("reassign_block", key, block_id, f, comp=comp)

    def assign_operator(self, agency, block_id, operator_id, key, *, compensation=False):
        def f():
            self.w.blocks[block_id].operator_id = operator_id
            return Result.success(self.w.blocks[block_id])
        return self._run("assign_operator", key, block_id, f, comp=compensation or "comp:" in key)

    def publish_rider_alert(self, agency, block_id, message, key):
        def f():
            self.w.alerts.append({"block": block_id, "message": message})
            return Result.success({"id": f"alert-{len(self.w.alerts)}"})
        return self._run("publish_rider_alert", key, block_id, f)


class FakeTools:
    """ToolSet-like bundle: `.reads` and `.writes`."""

    def __init__(self, world: FakeWorld):
        self.world = world
        self.reads = FakeReadTools(world)
        self.writes = FakeWriteTools(world)


class ScriptedReasoner:
    """Returns queued raw outputs (one per call), then delegates to the stub. Counts calls."""
    name = "scripted"
    prompt_version = "test"

    def __init__(self, outputs: list):
        from transitos.workflow.reasoning import StubReasoner
        self.outputs = list(outputs)
        self.calls = 0
        self._stub = StubReasoner()

    def propose(self, facts: dict, attempt: int = 0):
        self.calls += 1
        if self.outputs:
            o = self.outputs.pop(0)
            if isinstance(o, Exception):
                raise o
            return o
        return self._stub.propose(facts, attempt)


def make_event(type_: str, id_: str, data: dict, agency: str = AGENCY, t: datetime = T0) -> CloudEvent:
    return CloudEvent(id=id_, source=f"adapter/test/{agency}", type=type_, time=t, agencyid=agency, data=data)


def fault_event(vehicle: str = "veh-1", id_: str = "fault:1") -> CloudEvent:
    return make_event("VehicleFaultDetected", id_, {
        "native_vehicle_id": "T-77", "spn": 100, "fmi": 1, "lamps": {"stop": True},
        "refs": {"canonical": vehicle}})


def wo_closed_event(vehicle: str = "veh-1", id_: str = "wo-close:1", wo_no: str = "1001") -> CloudEvent:
    return make_event("WorkOrderClosed", id_, {"native_asset_id": "1106", "wo_no": wo_no,
                                              "refs": {"canonical": vehicle}})


def default_policy(**kw) -> AgencyPolicy:
    return AgencyPolicy(agency=AGENCY, **kw)
