"""Phase 10 Step 1 end to end, on FakeWorld and (when present) the real integrated system.

The spare-reuse cases below are the plan's "one table-driven test of the same cases against all four places":
the workflow check (do_check), the reasoning validator, the write adapter's re-check and the verification
tripwire all sit on the path of one run, so a disagreement between them shows up as a failed scenario."""
from __future__ import annotations

from dataclasses import replace

import pytest

from transitos.verification.invariants import check_all
from transitos.verification.runner import run_scenario
from transitos.verification.scenarios import BY_ID, Outcome, Scenario
from transitos.verification.scenarios_step1 import Step1Tweaks

FULL = (("deliver", 0), ("approve", "plan"), ("advance", 72), ("close_wo",), ("approve", "return_to_service"))


def variant(base: str, **kw) -> Scenario:
    return replace(BY_ID[base], **kw)


def distinct_spares(obs) -> set[str]:
    return {w.params["to_vehicle_id"] for w in obs.forward_writes() if w.action == "reassign_block" and w.ok}


@pytest.mark.parametrize("case,start_h,spares,expected,n_distinct", [
    ("non_overlapping_blocks_one_spare", 6.0, 2, Outcome.PLAN_VALID, 1),
    ("overlapping_blocks_two_spares_breach_last_spare", 2.0, 2, Outcome.ESCALATE_NO_SAFE_OPTION, 0),
    ("overlapping_blocks_three_spares_use_two", 2.0, 3, Outcome.PLAN_VALID, 2),
])
def test_spare_reuse_rules_agree_in_every_place(make_world, case, start_h, spares, expected, n_distinct):
    sc = variant("spare_reused_across_blocks", id=case, expected=expected,
                 expect_wo_creates=1 if expected is Outcome.PLAN_VALID else 0,
                 tweaks=Step1Tweaks(second_block=True, second_block_start_h=start_h, spares=spares))
    res = run_scenario(make_world(), sc, keep_observation=True)
    assert res.passed, (res.mismatches, res.invariants)
    assert len(distinct_spares(res.observation)) == n_distinct
    assert check_all(res.observation) == []


def test_larger_class_fallback_writes_nothing_until_the_approver_acknowledges(make_world):
    w = make_world()
    sc = BY_ID["larger_class_fallback"]
    w.apply(sc)
    w.deliver(sc.events[0])
    rid = w.observe().runs[0].run_id
    run = w.observe().runs[0]
    fit = next(i for i in run.plan.clearance if i.rule == "vehicle_fit")
    assert (fit.status, fit.requires_ack, run.plan.vehicle_fit) == ("warn", True, "larger")
    w.approve(rid)                                      # no acknowledgement
    obs = w.observe()
    assert obs.writes == [] and obs.runs[0].status == "AWAITING_APPROVAL" and obs.runs[0].decision is None
    w.acknowledge(rid, ["vehicle_fit"])
    w.approve(rid)
    obs = w.observe()
    assert obs.writes and obs.runs[0].status == "WAITING_FOR_REPAIR"
    assert obs.runs[0].acknowledged == ["vehicle_fit"]
    assert check_all(obs) == []


def test_half_operator_fields_missing_lists_every_unverifiable_item_and_still_completes(make_world):
    res = run_scenario(make_world(), BY_ID["half_operator_fields_missing"], keep_observation=True)
    assert res.passed
    run = res.observation.runs[0]
    unv = {i.rule for i in run.plan.clearance if i.status == "unverifiable"}
    assert unv == {"operator_qualification", "operator_hours_week"}
    assert all(i.requires_ack for i in run.plan.clearance if i.status == "unverifiable")
    assert not [i for i in run.plan.clearance if i.status == "violation"]
    assert set(run.acknowledged) == unv and run.status == "DONE"


def test_missing_data_without_acknowledgement_does_not_execute(make_world):
    sc = variant("half_operator_fields_missing", script=(("deliver", 0), ("approve", "plan")))
    res = run_scenario(make_world(), sc, keep_observation=True)
    assert res.observation.writes == [] and res.observation.runs[0].status == "AWAITING_APPROVAL"


@pytest.mark.parametrize("sid", ["expired_training", "endorsement_missing", "weekly_hours_cap",
                                 "length_restriction_blocks_larger", "vehicle_fit_exact_excludes_larger"])
def test_excluded_options_never_write(make_world, sid):
    res = run_scenario(make_world(), BY_ID[sid], keep_observation=True)
    assert res.passed and res.observation.writes == [] and res.observation.runs[0].status == "ESCALATED"
    assert res.observation.runs[0].violations


def test_larger_class_fallback_plan_is_marked_as_the_larger_tier(make_world):
    """Only larger-class spares exist: the plan is the fallback tier and says so."""
    res = run_scenario(make_world(), BY_ID["larger_class_fallback"], keep_observation=True)
    assert res.observation.runs[0].plan.vehicle_fit == "larger"
