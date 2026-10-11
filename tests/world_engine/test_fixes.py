"""Step 3 fix round (reviewer R3): honest latency, known gaps shown as such, the callout swap enforced by the report,
sub-second precision, retry safety, tie order, a lag-collision scenario, and FakeWorld skips that say why."""
import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from transitos.integration.world import make_world
from transitos.mocks.clock import SimClock
from transitos.verification import report
from transitos.verification.fake_world import FakeWorld
from transitos.verification.latency import LATENCY_NOTE
from transitos.verification.runner import drive, run_scenario
from transitos.verification.scenarios import BY_ID, SCENARIOS
from transitos.verification.scenarios_step3 import callout_day
from transitos.world.engine import WorldEngine
from transitos.world.lag import ProductLag
from transitos.world.ports import RecordingPorts
from transitos.world.script import DayScript
from transitos.world.truth import GroundTruth

DAY = datetime(2026, 10, 10, tzinfo=UTC)


def at(h, m=0, s=0):
    return DAY + timedelta(hours=h, minutes=m, seconds=s)


def real(sid, **replace):
    sc = BY_ID[sid]
    return report.run_all(make_world, (dataclasses.replace(sc, **replace) if replace else sc,))[0]


# ---------------------------------------------------------------- F1
def test_latency_caption_is_in_the_report_output_and_says_what_the_numbers_are():
    rs = report.run_all(make_world, (BY_ID["scripted_day_coolant"],))
    txt = report.format_latency(rs)
    assert LATENCY_NOTE in txt
    assert "by construction" in txt and "scripted human approval" in txt and "measurement of system speed" in txt
    assert "truth_to_approval_s" in txt and "approval_to_pull_s" in txt


def test_json_carries_a_latency_note_and_the_decomposed_numbers():
    rs = report.run_all(make_world, (BY_ID["severe_fault_with_spare"], BY_ID["scripted_day_coolant"]))
    by = {s["id"]: s for s in report.to_json(rs)["scenarios"]}
    assert by["severe_fault_with_spare"]["latency_note"] is None and by["severe_fault_with_spare"]["latency"] is None
    s = by["scripted_day_coolant"]
    assert s["latency_note"] == LATENCY_NOTE
    lat = s["latency"]
    assert (lat["truth_to_first_proposal_s"], lat["truth_to_approval_s"], lat["truth_to_pull_s"],
            lat["approval_to_pull_s"]) == (5.0, 60.0, 60.0, 0.0)
    assert lat["per_vehicle"]["veh-1"]["truth_to_pull_s"] == 60.0


def test_moving_the_approve_op_changes_truth_to_approval_not_truth_to_first_proposal():
    sc = BY_ID["scripted_day_coolant"]
    moved = dataclasses.replace(sc, script=(("world_until", "09:45"),) + sc.script[1:])
    a, b = real("scripted_day_coolant").latency, report.run_all(make_world, (moved,))[0].latency
    assert a["truth_to_first_proposal_s"] == b["truth_to_first_proposal_s"] == 5.0
    assert (a["truth_to_approval_s"], b["truth_to_approval_s"]) == (60.0, 300.0)
    assert b["truth_to_pull_s"] == 300.0


# ---------------------------------------------------------------- F2
def test_intermittent_fault_clears_is_a_pass_with_a_visible_known_gap():
    sc = BY_ID["intermittent_fault_clears"]
    assert "A100" in sc.known_gap and "A101" in sc.known_gap and "NOT tested" in sc.known_gap
    rs = report.run_all(make_world, (sc,))
    assert rs[0].passed and "PASS (known gap: " in report.format_table(rs)
    assert report.to_json(rs)["scenarios"][0]["known_gap"] == sc.known_gap


def test_scenarios_without_a_known_gap_print_plain_pass():
    rs = report.run_all(make_world, (BY_ID["scripted_day_coolant"],))
    assert "known gap" not in report.format_table(rs) and report.to_json(rs)["scenarios"][0]["known_gap"] == ""


def test_approved_while_the_fault_is_clear_the_orchestrator_still_pulls_the_bus():
    sc = BY_ID["intermittent_approved_while_clear"]
    assert sc.expected.value == "plan-valid" and sc.known_gap
    assert "cannot know" in sc.known_gap and "A101" in sc.known_gap and "never plans MONITOR" in sc.known_gap
    approve_at = next(op[1] for op, nxt in zip(sc.script, sc.script[1:], strict=False) if nxt[0] == "approve")
    assert approve_at == "08:07"
    # ground truth at that instant: the fault cleared at 08:06 and has not recurred
    w = make_world()
    w.apply(dataclasses.replace(sc, script=()))
    w.advance_world_to("08:07")
    assert w.engine.truth.state("veh-1") == "ok"
    r = run_scenario(make_world(), sc, keep_observation=True)
    assert r.passed, r.mismatches
    acts = [x.action for x in r.observation.forward_writes() if x.ok]
    assert "set_vehicle_unavailable" in acts and "create_work_order" in acts            # pulled a bus that was fine


# ---------------------------------------------------------------- F3
def test_callout_expectation_fails_the_report_row_when_the_swap_does_not_happen():
    sc = BY_ID["callout_with_extra_board"]
    assert sc.expect_forward_actions and "assign_operator" in sc.expect_forward_actions
    assert real("callout_with_extra_board").passed
    stale = dataclasses.replace(sc, script=(("world_until", "06:16"), ("approve", "plan")),
                                tweaks=dataclasses.replace(sc.tweaks, world_script=callout_day(fault_min=15)))
    r = report.run_all(make_world, (stale,))[0]
    assert not r.passed and any("assign_operator" in m for m in r.mismatches)


def test_expect_forward_actions_defaults_to_not_checked():
    assert all(s.expect_forward_actions is None for s in SCENARIOS if s.id not in ("callout_with_extra_board",))


# ---------------------------------------------------------------- F7
def test_sub_second_telematics_lag_is_not_truncated_in_the_proposal_time():
    sc = BY_ID["scripted_day_coolant"]
    fast = dataclasses.replace(sc, tweaks=dataclasses.replace(sc.tweaks, world_lag=ProductLag(telematics_s=0.7)))
    lat = report.run_all(make_world, (fast,))[0].latency
    assert lat["truth_to_telematics_s"] == 0.7 and lat["truth_to_first_proposal_s"] == 0.7


# ---------------------------------------------------------------- F8
class FlakyPorts(RecordingPorts):
    def __init__(self, clock):
        super().__init__(clock)
        self.fail_once = True

    def telematics_fault(self, vehicle, params):
        if self.fail_once:
            self.fail_once = False
            raise ConnectionError("telematics down")
        super().telematics_fault(vehicle, params)


def test_a_failing_port_call_does_not_lose_the_other_due_reactions():
    clock = SimClock(at(6))
    ports = FlakyPorts(clock)
    eng = WorldEngine(GroundTruth(clock, DayScript.example_coolant_fault(DAY), seed=1), ProductLag(), ports, clock)
    clock.advance(hours=6)                                  # 12:00: everything is due at once
    with pytest.raises(ConnectionError):
        eng.tick()
    eng.tick()                                              # retry
    eng.tick()
    kinds = sorted((o.product, o.kind) for o in eng.observations_log)
    assert kinds == sorted([("telematics", "fault_on"), ("scheduling", "fault_on"), ("maintenance", "fault_on"),
                            ("telematics", "fault_off"), ("maintenance", "return_to_service")])
    assert [c[0] for c in ports.calls].count("telematics_fault") == 1                  # performed once, after the retry


# ---------------------------------------------------------------- F10 (tie order)
def test_reactions_due_at_the_same_instant_run_in_actor_order():
    clock = SimClock(at(6))
    ports = RecordingPorts(clock)
    lag = ProductLag(telematics_s=60, shop_rule_min=1, dispatcher_min=1)                # all three due 09:41:00
    eng = WorldEngine(GroundTruth(clock, DayScript.example_coolant_fault(DAY), seed=1), lag, ports, clock)
    eng.run_until(at(9, 42))
    assert [c[0] for c in ports.calls] == ["telematics_fault", "maintenance_open", "scheduling_out_of_service"]


# ---------------------------------------------------------------- F4 + FakeWorld
def test_dispatcher_flip_before_approval_yields_plan_v2_then_executes_it():
    sc = BY_ID["dispatcher_acts_before_approval"]
    assert sc.real_world_only and sc.expect_plan_version == 2 and sc.expected.value == "plan-valid"
    r = run_scenario(make_world(), sc, keep_observation=True)
    assert r.passed, (r.mismatches, {k: v for k, v in r.invariants.items() if v})
    assert r.observation.runs[0].plan_version == 2
    assert any(o.product == "scheduling" and o.visible_at == at(9, 52) for o in make_world_obs(sc))


def make_world_obs(sc):
    w = make_world()
    drive(w, sc)
    return w.engine.observations_log


def test_fake_world_skips_lag_scenarios_and_says_why_it_does_not_fake_agreement():
    sc = BY_ID["dispatcher_acts_before_approval"]
    rs = report.run_all(FakeWorld, (sc,))
    assert rs[0].skipped and "lag" in rs[0].skipped and rs[0].passed
    txt = report.format_table(rs)
    assert "SKIP" in txt and rs[0].skipped in txt and "1 skipped" in txt
    assert report.to_json(rs)["scenarios"][0]["skipped"] == rs[0].skipped


def test_only_the_lag_scenarios_are_real_world_only():
    assert {s.id for s in SCENARIOS if s.real_world_only} == {"dispatcher_acts_before_approval"}
