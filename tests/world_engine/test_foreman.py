"""Phase 10 Step 4.1: the ForemanActor (Track A, world engine). Expected rows from the spec text: the foreman converts a
pending repair to a work order after a delay, sometimes puts it in WAITING_PARTS (25%), sometimes defers it back to
PENDING (10%); it never acts before its delay, never touches a work order someone else opened, never closes one
(the shop closes on return to service, A92), and draws are seeded per pending repair."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from transitos.data.loaders import load_shop_behaviour
from transitos.mocks.clock import SimClock
from transitos.world.actors import ForemanActor
from transitos.world.engine import WorldEngine
from transitos.world.lag import ProductLag
from transitos.world.ports import RecordingPorts
from transitos.world.script import DayScript
from transitos.world.truth import GroundTruth

DAY = datetime(2026, 10, 10, tzinfo=UTC)


def at(h, m=0, s=0):
    return DAY + timedelta(hours=h, minutes=m, seconds=s)


def engine(foreman, start=None):
    clock = SimClock(start or at(9))
    ports = RecordingPorts(clock)
    truth = GroundTruth(clock, DayScript("agency-a", ()), seed=0)
    eng = WorldEngine(truth, ProductLag(), ports, clock, actors=[foreman])
    return eng, ports, clock


def shop_calls(ports):
    return [c for c in ports.calls if c[0] in ("maintenance_convert", "maintenance_set_status")]


def test_converts_exactly_after_the_delay_and_never_before():
    eng, ports, clock = engine(ForemanActor(delay_min=30, parts_wait_prob=0.0, seed=1, defer_prob=0.0))
    ports.add_pending("pr-1", "veh-1")
    eng.run_until(at(9, 29, 59))
    assert shop_calls(ports) == []
    eng.run_until(at(9, 30))
    assert shop_calls(ports) == [("maintenance_convert", "pr-1", at(9, 30)),
                                 ("maintenance_set_status", "wo-pr-1", "IN_PROGRESS", at(9, 30))]
    eng.run_until(at(23))
    assert len(shop_calls(ports)) == 2                     # nothing else: no parts wait, no deferral, no close


def test_parts_wait_then_back_in_progress():
    eng, ports, _ = engine(ForemanActor(delay_min=10, parts_wait_prob=1.0, seed=1, defer_prob=0.0, parts_hours=3.0))
    ports.add_pending("pr-1", "veh-1")
    eng.run_until(at(20))
    assert shop_calls(ports) == [
        ("maintenance_convert", "pr-1", at(9, 10)),
        ("maintenance_set_status", "wo-pr-1", "IN_PROGRESS", at(9, 10)),
        ("maintenance_set_status", "wo-pr-1", "WAITING_PARTS", at(9, 10)),
        ("maintenance_set_status", "wo-pr-1", "IN_PROGRESS", at(12, 10))]


def test_deferral_back_to_pending_then_resumed():
    eng, ports, _ = engine(ForemanActor(delay_min=10, parts_wait_prob=0.0, seed=1, defer_prob=1.0, defer_hours=2.0))
    ports.add_pending("pr-1", "veh-1")
    eng.run_until(at(20))
    assert [c[2:] for c in shop_calls(ports)[1:]] == [
        ("IN_PROGRESS", at(9, 10)), ("PENDING", at(9, 10)), ("IN_PROGRESS", at(11, 10))]


def test_parts_wait_and_deferral_follow_each_other():
    eng, ports, _ = engine(ForemanActor(delay_min=0, parts_wait_prob=1.0, seed=1, defer_prob=1.0, parts_hours=1.0,
                                        defer_hours=2.0))
    ports.add_pending("pr-1", "veh-1")
    eng.run_until(at(20))
    assert [c[2:] for c in shop_calls(ports)[1:]] == [
        ("IN_PROGRESS", at(9)), ("WAITING_PARTS", at(9)), ("IN_PROGRESS", at(10)), ("PENDING", at(10)),
        ("IN_PROGRESS", at(12))]


def test_a_request_attached_to_a_work_order_someone_else_opened_is_left_to_them():
    eng, ports, _ = engine(ForemanActor(delay_min=10, parts_wait_prob=1.0, seed=1, defer_prob=1.0))
    ports.add_pending("pr-1", "veh-1")
    ports.open_records["veh-1"] = ["wo-human"]               # a person opened a work order first
    eng.run_until(at(20))
    assert shop_calls(ports) == [("maintenance_convert", "pr-1", at(9, 10))]
    assert any("someone else" in s.reason for s in eng.skipped)


def test_a_closed_work_order_is_not_touched_again():
    eng, ports, _ = engine(ForemanActor(delay_min=0, parts_wait_prob=1.0, seed=1, defer_prob=0.0, parts_hours=3.0))
    ports.add_pending("pr-1", "veh-1")
    eng.run_until(at(9))
    ports.wo_status["wo-pr-1"] = "CLOSED"                     # the shop signed it off early
    eng.run_until(at(20))
    assert ("maintenance_set_status", "wo-pr-1", "IN_PROGRESS", at(12)) not in shop_calls(ports)
    assert any("closed" in s.reason for s in eng.skipped)


def test_each_request_is_seen_once():
    eng, ports, _ = engine(ForemanActor(delay_min=5, parts_wait_prob=0.0, seed=1, defer_prob=0.0))
    ports.add_pending("pr-1", "veh-1")
    for m in range(0, 60, 7):
        eng.run_until(at(9, m))
    assert [c for c in shop_calls(ports) if c[0] == "maintenance_convert"] == [("maintenance_convert", "pr-1", at(9, 5))]


def test_defaults_come_from_shop_behaviour_and_are_seeded_per_request():
    sb = load_shop_behaviour()
    f = ForemanActor(seed=7)
    assert f.parts_wait_prob == sb.parts_wait.probability == 0.25
    assert f.defer_prob == sb.deferral_to_pending.probability == 0.10
    assert f.source == "config/shop_behaviour.yaml (ASSUMED, A119)"
    a, b = ForemanActor(seed=7).plan("pr-3"), ForemanActor(seed=7).plan("pr-3")
    assert a == b and a != ForemanActor(seed=8).plan("pr-3")
    assert a.delay > timedelta(0)


def test_long_run_frequencies_match_the_configured_probabilities():
    f = ForemanActor(seed=11)
    plans = [f.plan(f"pr-{i}") for i in range(4000)]
    parts = sum(p.parts is not None for p in plans) / len(plans)
    defer = sum(p.defer is not None for p in plans) / len(plans)
    assert parts == pytest.approx(0.25, abs=0.03) and defer == pytest.approx(0.10, abs=0.02)
    hours = sorted(p.delay.total_seconds() / 3600 for p in plans)
    assert hours[len(hours) // 2] == pytest.approx(4.0, rel=0.15)     # lognormal median 4 h (shop_behaviour)


def test_parameters_are_validated():
    for bad in ({"parts_wait_prob": 1.5}, {"defer_prob": -0.1}, {"delay_min": -1}):
        with pytest.raises(ValueError):
            ForemanActor(seed=1, **bad)
