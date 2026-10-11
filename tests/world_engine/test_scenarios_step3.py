"""Step 3.5 scenarios through the REAL path: real mock products, the real integrated System (world.apply ->
System -> workflow -> tools -> mocks) and the real verification report. Expected numbers are written from the
scripted days (scenarios_step3.py docstring) and the spec's lag rows, not read back from a run."""
import json

import pytest

from transitos.integration.world import make_world
from transitos.verification import report
from transitos.verification.runner import drive, run_scenario
from transitos.verification.scenarios import BY_ID, SCENARIOS

IDS = ("scripted_day_coolant", "intermittent_fault_clears", "callout_with_extra_board")


def result(sid):
    return report.run_all(make_world, (BY_ID[sid],))[0]


def test_registered_after_the_32_older_scenarios_without_touching_them():
    assert len(SCENARIOS) == 35 and [s.id for s in SCENARIOS[32:]] == list(IDS)
    assert BY_ID["scripted_day_coolant"].expected.value == "plan-valid"
    assert BY_ID["callout_with_extra_board"].expected.value == "plan-valid"


@pytest.mark.parametrize("sid", IDS)
def test_scenarios_pass_on_the_real_system_with_every_invariant(sid):
    r = result(sid)
    assert r.passed, (r.mismatches, {k: v for k, v in r.invariants.items() if v})


def test_scripted_coolant_day_latency_numbers_follow_the_script():
    # fault truth 09:40:00; telematics +5 s; the system starts and proposes when it processes that event (09:40:05);
    # the script approves at 09:41:00, which is when the pull write lands.
    lat = result("scripted_day_coolant").latency
    assert lat == {"truth_to_telematics_s": 5.0, "truth_to_first_proposal_s": 5.0, "truth_to_pull_s": 60.0}


def test_latency_is_in_the_json_report_per_scenario_and_does_not_change_pass_fail(tmp_path):
    rs = report.run_all(make_world, (BY_ID["severe_fault_with_spare"], BY_ID["scripted_day_coolant"]))
    doc = report.to_json(rs)
    by = {s["id"]: s for s in doc["scenarios"]}
    assert by["severe_fault_with_spare"]["latency"] is None            # no world engine: no truth to measure from
    assert by["scripted_day_coolant"]["latency"]["truth_to_telematics_s"] == 5.0
    assert all(s["passed"] for s in doc["scenarios"])
    txt = report.format_latency(rs)
    assert "scripted_day_coolant" in txt and "5.0" in txt and "60.0" in txt and "severe_fault_with_spare" not in txt


def test_scripted_day_is_byte_identical_across_runs():
    outs = []
    for _ in range(2):
        w = make_world()
        drive(w, BY_ID["scripted_day_coolant"])
        outs.append(w.timeline_json())
    assert outs[0] == outs[1]
    doc = json.loads(outs[0])
    assert [o["product"] for o in doc["observations"]][:1] == ["telematics"]
    assert doc["products"]["maintenance"]                               # the products' own records are in it too


def test_scripted_coolant_day_what_each_product_ended_up_holding():
    w = make_world()
    obs = drive(w, BY_ID["scripted_day_coolant"])
    run = obs.runs[0]
    assert run.status == "DONE" and run.vehicle_id == "veh-1"
    log = {(o.product, o.kind): o for o in w.engine.observations_log}
    assert log[("telematics", "fault_on")].visible_at.isoformat() == "2026-10-10T09:40:05+00:00"
    # the orchestrator pulled the vehicle at 09:41 and opened the WO, so the dispatcher (09:52) and the shop rule
    # (10:00) found the work already done and did nothing
    assert ("scheduling", "fault_on") not in log and ("maintenance", "fault_on") not in log
    assert {(s.product, s.kind) for s in w.engine.skipped} >= {("scheduling", "fault_on"), ("maintenance", "fault_on")}
    assert log[("maintenance", "return_to_service")].visible_at.isoformat() == "2026-10-10T11:50:00+00:00"


def test_intermittent_fault_recurrence_is_attached_to_the_one_run():
    r = result("intermittent_fault_clears")
    assert r.outcome == "plan-valid"
    obs = run_scenario(make_world(), BY_ID["intermittent_fault_clears"], keep_observation=True).observation
    assert len(obs.runs) == 1
    assert sum(len(v) for v in obs.attached_events.values()) == 1        # the recurrence
    assert len({w.effect_id for w in obs.forward_writes() if w.action == "create_work_order"}) == 1


def test_callout_with_extra_board_swaps_in_the_extra_board_operator():
    w = make_world()
    obs = drive(w, BY_ID["callout_with_extra_board"])
    swaps = [x for x in obs.forward_writes() if x.action == "assign_operator" and x.ok]
    assert [x.params["operator_id"] for x in swaps] == ["op-2"]           # OP-1 called out; OP-2 is the extra board
    cal = next(o for o in w.engine.observations_log if o.kind == "callout")
    assert (cal.visible_at - cal.truth_at).total_seconds() == 12 * 60      # the dispatcher takes 12 min to record it


def test_lag_matters_a_plan_made_before_the_callout_is_recorded_keeps_the_called_out_operator():
    """Same day as the callout scenario but the fault lands 5 minutes after the call-out beat, so the workforce
    product still shows OP-1 available when the run gathers its facts (the dispatcher records it 12 min later)."""
    import dataclasses

    from transitos.verification.scenarios_step3 import callout_day
    sc = BY_ID["callout_with_extra_board"]
    early = dataclasses.replace(sc, id="callout_stale", script=(("world_until", "06:16"), ("approve", "plan")),
                                tweaks=dataclasses.replace(sc.tweaks, world_script=callout_day(fault_min=15)))
    w = make_world()
    obs = drive(w, early)
    assert not [x for x in obs.forward_writes() if x.action == "assign_operator"]


def test_original_scenarios_are_untouched():
    assert [s.id for s in SCENARIOS[:5]] == ["severe_fault_with_spare", "operator_swap_needed", "pm_overdue_urgent",
                                             "pm_overdue_minor", "dvir_defect"]
    assert not any(hasattr(s.tweaks, "world_script") for s in SCENARIOS[:32])
