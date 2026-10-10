"""Level 2 (act with approval) acceptance tests, written against the World interface.

The original spec is not in the repo; these 12 are derived from CLAUDE.md rules + the Track F brief
(see handoff F.md).  Each runs on FakeWorld now and on the real World when provided (else skipped).
"""
import pytest

from transitos.contracts.tools import SAGA_ORDER
from transitos.verification.invariants import check_all
from transitos.verification.model import RETURN_ACTION
from transitos.verification.runner import run_scenario
from transitos.verification.scenarios import BY_ID, SCENARIOS


def go(make_world, scenario_id):
    res = run_scenario(make_world(), BY_ID[scenario_id], keep_observation=True)
    assert res.mismatches == [], res.mismatches
    obs = res.observation
    assert check_all(obs) == []          # every acceptance scenario also satisfies every invariant
    return obs


def test_01_happy_path_five_steps_in_saga_order_then_return(make_world):
    obs = go(make_world, "severe_fault_with_spare")
    fw = [w.action for w in obs.forward_writes() if w.ok and w.action != RETURN_ACTION]
    assert fw == SAGA_ORDER
    assert fw[-1] == "publish_rider_alert"
    assert obs.forward_writes()[-1].action == RETURN_ACTION
    assert obs.runs[0].status == "DONE"


def test_02_nothing_is_written_before_approval(make_world):
    from transitos.verification.scenarios import Scenario
    sc = Scenario(**{**BY_ID["severe_fault_with_spare"].__dict__, "script": (("deliver", 0),)})
    res = run_scenario(make_world(), sc, keep_observation=True)
    assert res.observation.writes == []
    assert res.observation.runs[0].status == "AWAITING_APPROVAL"
    assert res.observation.runs[0].plan.steps


@pytest.mark.parametrize("sid", ["last_spare", "no_feasible_spare"])
def test_03_spare_hard_limits_escalate_with_zero_writes(make_world, sid):
    obs = go(make_world, sid)
    assert obs.writes == [] and obs.runs[0].status == "ESCALATED" and obs.runs[0].violations


@pytest.mark.parametrize("sid", ["wheelchair_mismatch", "lift_mismatch"])
def test_04_accessibility_mismatch_never_reassigns(make_world, sid):
    obs = go(make_world, sid)
    assert not [w for w in obs.writes if w.action == "reassign_block"]


@pytest.mark.parametrize("sid", ["operator_hours_exhausted", "unqualified_operator", "lift_untrained_operator"])
def test_05_operator_limits_never_assign(make_world, sid):
    obs = go(make_world, sid)
    assert not [w for w in obs.writes if w.action == "assign_operator"]
    assert obs.runs[0].status == "ESCALATED"


def test_06_duplicate_trigger_makes_one_run_and_one_wo(make_world):
    obs = go(make_world, "duplicate_trigger")
    assert len(obs.runs) == 1
    assert len({w.effect_id for w in obs.writes if w.action == "create_work_order"}) == 1


def test_07_second_trigger_on_active_vehicle_attaches_not_new_run(make_world):
    obs = go(make_world, "trigger_vehicle_active_run")
    assert len(obs.runs) == 1
    assert sum(len(v) for v in obs.attached_events.values()) == 1


def test_08_human_work_order_is_reused(make_world):
    obs = go(make_world, "human_wo_exists")
    assert not [w for w in obs.writes if w.action == "create_work_order"]
    assert obs.runs[0].wo_id is not None


def test_09_failed_write_compensates_in_reverse_and_never_alerts(make_world):
    obs = go(make_world, "write_rejected_mid_plan")
    forward_ok = [w for w in obs.writes if not w.compensation and w.ok]
    comps = [w for w in obs.writes if w.compensation]
    assert [c.compensates_key for c in comps] == [w.key for w in reversed(forward_ok)]
    assert not obs.alerts and not [w for w in obs.writes if w.action == "publish_rider_alert"]
    assert obs.runs[0].status == "ESCALATED"


def test_10_kill_switch_stops_all_further_writes(make_world):
    obs = go(make_world, "kill_switch_mid_run")
    kill = next(a for a in obs.audit if a.kind == "kill_switch" and a.data["on"])
    assert obs.writes and all(w.seq < kill.seq for w in obs.writes)
    assert not obs.alerts


@pytest.mark.parametrize("sid,status", [("approval_rejected", "CLOSED_REJECTED"), ("approval_expired", "ESCALATED")])
def test_11_reject_or_timeout_means_zero_writes(make_world, sid, status):
    obs = go(make_world, sid)
    assert obs.writes == [] and obs.runs[0].status == status


@pytest.mark.parametrize("sid", ["product_down_on_read", "unresolved_vehicle_id"])
def test_12_unavailable_or_unresolvable_input_is_never_guessed(make_world, sid):
    obs = go(make_world, sid)
    assert obs.writes == []


@pytest.mark.parametrize("sid", [s.id for s in SCENARIOS])
def test_all_scenarios_meet_expected_outcome_and_invariants(make_world, sid):
    res = run_scenario(make_world(), BY_ID[sid])
    assert res.passed, (res.mismatches, res.invariants)
