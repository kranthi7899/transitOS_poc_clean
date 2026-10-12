"""Bug sweep B fix round (R3), world engine. Expected rows are written from the ruling text.

B1 (A168): the dispatcher puts a bus it pulled back in service only when maintenance has NO open work order or
pending repair for it; while one is open it re-checks at every engine step and restores as soon as it sees the record
closed (never earlier, never before return_to_service + its own lag).
B2 (A169): a bus this dispatcher never pulled leaves no `skipped` row at return_to_service."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from transitos.integration.system import Mocks
from transitos.mocks.clock import SimClock
from transitos.mocks.seed import build_fleet
from transitos.world.actors import DispatcherActor
from transitos.world.engine import WorldEngine
from transitos.world.lag import ProductLag
from transitos.world.ports import MockPorts, RecordingPorts
from transitos.world.script import Beat, DayScript
from transitos.world.truth import GroundTruth

AG = "agency-042"
H = {"X-Agency": AG}
DAY = datetime(2026, 10, 10, tzinfo=UTC)
STOP = {"spn": 110, "fmi": 0, "lamps": {"stop": True, "warn": True}}


def at(h, m=0):
    return DAY + timedelta(hours=h, minutes=m)


def _rig(script):
    clock = SimClock(at(4))
    mocks = Mocks.start(clock)
    mocks.seed(AG)
    eng = WorldEngine(GroundTruth(clock, script, seed=1), ProductLag(),
                      MockPorts(mocks, AG, build_fleet(AG, clock.now())), clock)
    return mocks, eng


def _sched(mocks, native):
    return [(c["status"], c["changed_at"], c["changed_by"])
            for c in mocks.clients["scheduling"].get("/status-changes", headers=H).json() if c["vehicle"] == native]


# ------------------------------------------------------------------ B1: the reviewer's coolant-day repro
def test_coolant_day_bus_goes_back_in_service_only_when_the_work_order_is_closed():
    mocks, eng = _rig(DayScript.example_coolant_fault(DAY))
    eng.run_until(at(12, 30))
    (wo,) = mocks.clients["maintenance"].get("/workorders?asset=1108", headers=H).json()
    assert (wo["status"], wo["opened_at"], wo["closed_at"]) == ("closed", "2026-10-10T10:00:00Z",
                                                                "2026-10-10T11:50:00Z")   # shop: +20 min
    assert _sched(mocks, "BUS-46") == [("out_of_service", "2026-10-10T09:52:00Z", "dispatcher-sim"),
                                       ("in_service", "2026-10-10T11:50:00Z", "dispatcher-sim")]   # not 11:42
    back = [(o.kind, o.truth_at, o.visible_at) for o in eng.observations_log if o.product == "scheduling"]
    assert back == [("fault_on", at(9, 40), at(9, 52)), ("return_to_service", at(11, 30), at(11, 50))]
    assert not [s for s in eng.skipped if s.product == "scheduling"]


def test_between_return_and_the_close_the_bus_stays_out_of_service():
    mocks, eng = _rig(DayScript.example_coolant_fault(DAY))
    eng.run_until(at(11, 49))
    assert mocks.clients["scheduling"].get("/vehicles/BUS-46", headers=H).json()["status"] == "out_of_service"
    assert [w["status"] for w in mocks.clients["maintenance"].get("/workorders?asset=1108", headers=H).json()] == ["open"]


def test_an_open_pending_repair_also_keeps_the_bus_out():
    """A pending repair (not yet on a work order) is an open maintenance record for the asset too."""
    ports = RecordingPorts()
    d = DispatcherActor(ProductLag())
    pull = Beat(at(9, 40), "veh-3", "fault_on", {**STOP})
    back = Beat(at(11, 30), "veh-3", "return_to_service", {})
    assert d.act(pull, ports) is None
    ports.pending["pr-1"] = "veh-3"
    assert d.act(back, ports) is not None and ports.status["veh-3"] == "out_of_service"   # deferred, not restored
    assert d.discover(at(12), ports) == []                                 # still open: nothing to do yet
    del ports.pending["pr-1"]
    ((due, beat),) = d.discover(at(13), ports)                             # seen closed: re-act now
    assert due == at(13) and beat == back
    assert d.act(back, ports) is None and ports.status["veh-3"] == "in_service"


def test_a_work_order_that_stays_open_keeps_the_bus_out_until_it_closes():
    """Engine level: the dispatcher alone, a record that stays open until 15:00, no other actor."""
    clock = SimClock(at(6))
    ports = RecordingPorts(clock)
    script = DayScript(AG, (Beat(at(9, 40), "veh-3", "fault_on", {**STOP}),
                            Beat(at(11, 30), "veh-3", "return_to_service", {})))
    eng = WorldEngine(GroundTruth(clock, script, seed=1), ProductLag(), ports, clock,
                      actors=[DispatcherActor(ProductLag())])
    ports.open_records["veh-3"] = ["wo-1"]
    eng.run_until(at(14, 59))
    assert ports.status["veh-3"] == "out_of_service"
    assert not [o for o in eng.observations_log if o.kind == "return_to_service"] and not eng.skipped
    ports.open_records["veh-3"] = []                                       # someone closes it at 14:59
    eng.run_until(at(15))
    # the dispatcher sees the close at the engine's next look (the start of this step, 14:59) and acts then
    (o,) = [o for o in eng.observations_log if o.kind == "return_to_service"]
    assert (o.truth_at, o.visible_at) == (at(11, 30), at(14, 59)) and ports.status["veh-3"] == "in_service"


# ------------------------------------------------------------------ B2: no row for a bus this actor never pulled
def test_no_skip_row_for_a_bus_the_dispatcher_never_pulled():
    script = DayScript(AG, (Beat(at(11, 30), "veh-3", "return_to_service", {}),))
    mocks, eng = _rig(script)
    mocks.clients["scheduling"].post("/vehicles/BUS-46/unavailable", json={"changed_by": "system:run-1:pull:1"},
                                     headers=H)
    eng.run_until(at(12, 30))
    assert [c[0] for c in _sched(mocks, "BUS-46")] == ["out_of_service"]
    assert eng.skipped == [] and not [o for o in eng.observations_log if o.product == "scheduling"]


def test_a_bus_it_did_pull_but_someone_else_changed_still_logs_its_reason():
    script = DayScript(AG, (Beat(at(9, 40), "veh-3", "fault_on", {**STOP}),
                            Beat(at(11, 30), "veh-3", "return_to_service", {})))
    mocks, eng = _rig(script)
    eng.run_until(at(10))
    mocks.clients["scheduling"].post("/vehicles/BUS-46/unavailable", json={"changed_by": "system:run-1:pull:1"},
                                     headers=H)
    eng.run_until(at(12, 30))
    (s,) = [s for s in eng.skipped if s.product == "scheduling"]
    assert s.kind == "return_to_service" and s.at == at(11, 50) and "someone else" in s.reason
