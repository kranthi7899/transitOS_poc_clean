"""The world engine against the REAL mock products (in-process apps, shared SimClock): what each product actually
holds, and when it first held it, must match the lag rows in the Step 3 spec. No System is attached here; the
integrated path (system + report) is in tests/world/test_scenarios_step3.py."""
from datetime import UTC, datetime, timedelta

import pytest

from transitos.integration.system import Mocks
from transitos.mocks.clock import SimClock, parse_ts
from transitos.mocks.seed import build_fleet
from transitos.world.engine import WorldEngine
from transitos.world.lag import ProductLag
from transitos.world.ports import MockPorts
from transitos.world.script import Beat, DayScript
from transitos.world.stochastic import StochasticConfig, generate_world
from transitos.world.truth import GroundTruth

AG = "agency-042"
H = {"X-Agency": AG}
DAY = datetime(2026, 10, 10, tzinfo=UTC)


def at(h, m=0, s=0):
    return DAY + timedelta(hours=h, minutes=m, seconds=s)


@pytest.fixture
def rig():
    clock = SimClock(at(6))
    mocks = Mocks.start(clock)
    mocks.seed(AG)

    def build(script, lag=None):
        truth = GroundTruth(clock, script, seed=1)
        return WorldEngine(truth, lag or ProductLag(), MockPorts(mocks, AG, build_fleet(AG, clock.now())), clock)
    return mocks, clock, build


def test_example_day_lands_in_each_product_at_truth_plus_its_own_lag(rig):
    mocks, _, build = rig
    eng = build(DayScript.example_coolant_fault(DAY))
    eng.run_until(at(12))
    # veh-3 in agency-042: telematics T-79, scheduling BUS-46, maintenance asset 1108
    feed = mocks.clients["telematics"].get("/faults/feed", headers=H).json()["items"]
    assert [(e["fault"]["spn"], e["vehicle"]["id"], e["time"]) for e in feed] == [(110, "T-79", "2026-10-10T09:40:05Z")]
    changes = [c for c in mocks.clients["scheduling"].get("/status-changes", headers=H).json() if c["vehicle"] == "BUS-46"]
    # bug sweep B (A156) + fix round B1 (A168), deliberately: the dispatcher returns its pull when the shop closes the WO
    assert [(c["status"], c["changed_at"]) for c in changes] == [("out_of_service", "2026-10-10T09:52:00Z"),
                                                                 ("in_service", "2026-10-10T11:50:00Z")]   # B1: at close
    wos = mocks.clients["maintenance"].get("/workorders", headers=H).json()
    assert [(w["asset"], w["opened_at"], w["closed_at"], w["status"]) for w in wos] == [
        ("1108", "2026-10-10T10:00:00Z", "2026-10-10T11:50:00Z", "closed")]
    assert mocks.clients["telematics"].get("/faults/current", headers=H).json() == []      # cleared at 10:15:05
    for o in eng.observations_log:                                                         # the log agrees with the products
        assert o.visible_at >= o.truth_at


def test_no_product_holds_the_fault_before_its_lag_has_passed(rig):
    mocks, _, build = rig
    eng = build(DayScript.example_coolant_fault(DAY))
    eng.run_until(at(9, 41))                                                               # 09:41: only telematics (5 s)
    assert mocks.clients["telematics"].get("/faults/current", headers=H).json()
    assert not [c for c in mocks.clients["scheduling"].get("/status-changes", headers=H).json() if c["vehicle"] == "BUS-46"]
    assert mocks.clients["maintenance"].get("/workorders", headers=H).json() == []
    eng.run_until(at(9, 51, 59))
    assert mocks.clients["scheduling"].get("/vehicles/BUS-46", headers=H).json()["status"] == "in_service"
    assert mocks.clients["maintenance"].get("/workorders", headers=H).json() == []
    eng.run_until(at(9, 52))
    assert mocks.clients["scheduling"].get("/vehicles/BUS-46", headers=H).json()["status"] == "out_of_service"


def test_callout_lands_in_workforce_after_dispatcher_lag(rig):
    mocks, _, build = rig
    eng = build(DayScript(AG, (Beat(at(6, 10), "op-1", "callout", {"operator": "op-1"}),)))
    eng.run_until(at(7))
    ch = mocks.clients["workforce"].get("/availability-changes", headers=H).json()
    assert [(c["operator"], c["status"], c["changed_at"]) for c in ch] == [("OP-310", "unavailable", "2026-10-10T06:22:00Z")]


def test_dvir_beat_reaches_telematics_after_its_lag(rig):
    mocks, _, build = rig
    eng = build(DayScript(AG, (Beat(at(7), "veh-2", "dvir", {"defect_text": "door closes slowly"}),)))
    eng.run_until(at(8))
    feed = mocks.clients["telematics"].get("/faults/feed", headers=H).json()["items"]
    assert [(e["dvir"]["defect_text"], e["time"]) for e in feed] == [("door closes slowly", "2026-10-10T07:00:05Z")]


def test_a_stochastic_week_runs_through_the_real_products_and_never_early(rig):
    mocks, _, build = rig
    w = generate_world(StochasticConfig(seed=3, agency=AG, vehicles=tuple(f"veh-{n}" for n in range(1, 13)),
                                        operators=tuple(f"op-{n}" for n in range(1, 11)), start=DAY, days=400,
                                        revenue_miles_per_day=150.0, quantile="p10", fleet_cut="all"))
    assert any(b.kind == "fault_on" for b in w.script.beats)
    eng = build(w.script)
    eng.run_until(DAY + timedelta(days=402))
    lag = ProductLag()
    due = {"telematics": timedelta(seconds=lag.telematics_s), "maintenance": timedelta(minutes=lag.shop_rule_min),
           "scheduling": timedelta(minutes=lag.dispatcher_min), "workforce": timedelta(minutes=lag.dispatcher_min)}
    assert eng.observations_log
    for o in eng.observations_log:
        assert o.visible_at >= o.truth_at + due[o.product]
    n_events = len(mocks.clients["telematics"].get("/faults/feed", headers=H).json()["items"])
    assert n_events == sum(1 for o in eng.observations_log if o.product == "telematics" and o.kind in ("fault_on", "dvir"))
    assert all(parse_ts(w_["opened_at"]) >= DAY for w_ in mocks.clients["maintenance"].get("/workorders", headers=H).json())
