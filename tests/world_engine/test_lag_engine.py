"""Step 3.2 per-product lag + actors, against an in-memory port first (the real mocks are in test_real_mocks.py).
Expected rows come from the spec: ProductLag(telematics_s=5, shop_rule_min=20, dispatcher_min=12); telematics sees
the fault after telematics_s, the shop creates the record after shop_rule_min, the dispatcher flips scheduling after
dispatcher_min and only when not already out of service."""
import json
from datetime import UTC, datetime, timedelta

from transitos.mocks.clock import SimClock
from transitos.world.engine import Observation, WorldEngine
from transitos.world.lag import ProductLag
from transitos.world.ports import RecordingPorts
from transitos.world.script import Beat, DayScript
from transitos.world.truth import GroundTruth

DAY = datetime(2026, 10, 10, tzinfo=UTC)


def at(h, m=0, s=0):
    return DAY + timedelta(hours=h, minutes=m, seconds=s)


def engine(script=None, lag=None, ports=None, start=None):
    clock = SimClock(start or at(6))
    truth = GroundTruth(clock, script or DayScript.example_coolant_fault(DAY), seed=1)
    ports = ports or RecordingPorts(clock)
    return WorldEngine(truth, lag or ProductLag(), ports, clock), ports, clock


def rows(eng):
    return [(o.product, o.vehicle, o.kind, o.truth_at, o.visible_at) for o in eng.observations_log]


def test_default_lag_values():
    lag = ProductLag()
    assert (lag.telematics_s, lag.shop_rule_min, lag.dispatcher_min) == (5, 20, 12)


def test_example_day_observation_rows_written_from_the_spec():
    eng, _, _ = engine()
    eng.run_until(at(12))
    assert sorted(rows(eng), key=lambda r: (r[4], r[0])) == [
        ("telematics", "veh-3", "fault_on", at(9, 40), at(9, 40, 5)),          # +5 s
        ("scheduling", "veh-3", "fault_on", at(9, 40), at(9, 52)),             # +12 min dispatcher
        ("maintenance", "veh-3", "fault_on", at(9, 40), at(10, 0)),            # +20 min shop rule
        ("telematics", "veh-3", "fault_off", at(10, 15), at(10, 15, 5)),
        ("maintenance", "veh-3", "return_to_service", at(11, 30), at(11, 50)),  # shop closes the record +20 min
        # bug sweep B (A156) added the dispatcher's return of the bus IT pulled; the fix round (B1, A168)
        # deliberately moved it from +12 min to the shop's close (+20 min): never back in service with an open record
        ("scheduling", "veh-3", "return_to_service", at(11, 30), at(11, 50)),
    ]


def test_each_product_is_delayed_by_its_own_lag_only():
    lag = ProductLag(telematics_s=1, shop_rule_min=3, dispatcher_min=7)
    eng, _, _ = engine(lag=lag)
    eng.run_until(at(12))
    got = {(o.product, o.kind): o.visible_at - o.truth_at for o in eng.observations_log}
    assert got[("telematics", "fault_on")] == timedelta(seconds=1)
    assert got[("scheduling", "fault_on")] == timedelta(minutes=7)
    assert got[("maintenance", "fault_on")] == timedelta(minutes=3)
    assert got[("maintenance", "return_to_service")] == timedelta(minutes=3)


def test_engine_never_emits_before_truth_plus_lag_even_with_coarse_ticks():
    lag = ProductLag()
    eng, ports, clock = engine()
    clock.advance(hours=3, minutes=40)                # now exactly 09:40: nothing is due yet
    eng.tick()
    assert ports.calls == []                          # not even the telematics 5 s
    clock.advance(minutes=1)                          # a coarse step: 09:41, everything with lag <= 1 min is due
    eng.tick()
    assert [c[0] for c in ports.calls] == ["telematics_fault"]
    clock.advance(hours=5)                            # a big jump: everything else, late but never early
    eng.tick()
    due = {"telematics": timedelta(seconds=lag.telematics_s), "maintenance": timedelta(minutes=lag.shop_rule_min),
           "scheduling": timedelta(minutes=lag.dispatcher_min)}
    for o in eng.observations_log:
        assert o.visible_at >= o.truth_at + due[o.product]


def test_each_beat_is_observed_once_however_often_the_engine_ticks():
    eng, ports, _ = engine()
    eng.run_until(at(12))
    n = len(eng.observations_log)
    for _ in range(5):
        eng.tick()
    eng.run_until(at(12))
    eng.run_until(at(8))                              # earlier than now: no-op
    assert len(eng.observations_log) == n == 6        # bug sweep B (A156): + the dispatcher's return (was 5)
    assert [c[0] for c in ports.calls].count("telematics_fault") == 1


def test_dispatcher_does_not_flip_a_vehicle_that_is_already_out_of_service():
    eng, ports, _ = engine()
    ports.status["veh-3"] = "out_of_service"          # e.g. the orchestrator's approved pull landed first
    eng.run_until(at(12))
    assert not [o for o in eng.observations_log if o.product == "scheduling"]
    # fix round B2 (A169): deliberately back to the original row (sweep B had added a return_to_service skip): it did
    # not pull the bus, so its return leaves no row and the bus stays out
    assert [s.kind for s in eng.skipped] == ["fault_on"] and eng.skipped[0].product == "scheduling"
    assert ports.status["veh-3"] == "out_of_service"


def test_shop_does_not_open_a_second_record_when_one_is_open():
    eng, ports, _ = engine()
    ports.open_records["veh-3"] = ["wo-1"]            # e.g. the orchestrator's work order exists
    eng.run_until(at(12))
    assert not [o for o in eng.observations_log if (o.product, o.kind) == ("maintenance", "fault_on")]
    assert any(s.product == "maintenance" and s.kind == "fault_on" for s in eng.skipped)
    assert ("maintenance", "return_to_service") in {(o.product, o.kind) for o in eng.observations_log}   # still closes it


def test_callout_is_recorded_by_the_dispatcher_after_dispatcher_min():
    s = DayScript("agency-a", (Beat(at(6, 10), "op-1", "callout", {"operator": "op-1"}),))
    eng, _, _ = engine(s)
    eng.run_until(at(7))
    assert rows(eng) == [("workforce", "op-1", "callout", at(6, 10), at(6, 22))]


def test_observation_log_serialisation_is_byte_identical_across_runs():
    outs = []
    for _ in range(2):
        eng, _, _ = engine()
        eng.run_until(at(12))
        outs.append(eng.timeline_json())
    assert outs[0] == outs[1]
    assert json.loads(outs[0])[0]["product"] == "telematics"
    assert isinstance(eng.observations_log[0], Observation)
