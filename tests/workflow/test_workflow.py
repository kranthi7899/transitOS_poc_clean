from __future__ import annotations

import pytest

from transitos.contracts.objects import WorkOrder
from transitos.workflow.fakes import (
    AGENCY,
    ScriptedReasoner,
    SimulatedCrash,
    default_policy,
    fault_event,
    make_event,
    wo_closed_event,
)
from transitos.workflow.graph import run_config
from transitos.workflow.starter import handle_event, resume_approval, resume_return
from transitos.workflow.timers import expire_approvals

SAGA = ["create_work_order", "set_vehicle_unavailable", "reassign_block", "assign_operator", "publish_rider_alert"]


def start(env, ev=None):
    r = handle_event(ev or fault_event(), env.store, env.graph, env.clock)
    assert r.action == "started", r
    return r.run_id


def approve(env, rid, **kw):
    resume_approval(env.graph, env.store, rid, kw.pop("decision", "approve"), "alice", **kw)


def run_of(env, rid):
    return env.state(rid).values["run"]


def test_happy_path_five_steps(env):
    rid = start(env)
    assert env.store.get_run(rid)["status"] == "AWAITING_APPROVAL"
    assert env.world.effects == []  # nothing written before approval
    plan = run_of(env, rid)["plan"]
    assert [s["action"] for s in plan["steps"]] == SAGA
    assert plan["fallback_used"] is False
    approve(env, rid)
    assert env.world.effect_names() == SAGA
    assert env.store.get_run(rid)["status"] == "WAITING_FOR_REPAIR"
    assert env.world.blocks["B1"].vehicle_id == "veh-2"
    assert env.world.blocks["B1"].operator_id == "OP-1"  # OP-old (9.5h) and OP-unq are infeasible
    assert handle_event(wo_closed_event(), env.store, env.graph).action == "resumed"
    assert env.store.get_run(rid)["status"] == "RETURNING"
    resume_return(env.graph, rid, "approve", "alice")
    assert env.store.get_run(rid)["status"] == "DONE"
    assert env.world.vehicles["veh-1"].status == "ready"
    rec = env.store.get_decision(rid, 1)
    assert rec["outcome"] == "done" and rec["human_action"]["decision"] == "approve"
    assert env.store.active_run(AGENCY, "veh-1") is None


def test_interrupt_rerun_performs_no_writes(env):
    """Rule 5: re-entering the interrupted node (A4) does not write anything."""
    rid = start(env)
    env.graph.invoke(None, run_config(rid))
    assert env.world.calls == []


def test_reject_before_writes_closes(env):
    rid = start(env)
    approve(env, rid, decision="reject")
    assert env.store.get_run(rid)["status"] == "CLOSED_REJECTED"
    assert env.world.effects == []


def test_reject_mid_plan_reverse_undo_then_escalate(make_env):
    pol = default_policy()
    pol.autonomy["create_work_order"] = "act"
    pol.autonomy["set_vehicle_unavailable"] = "act"
    env = make_env(policy=pol)
    rid = start(env)
    assert env.world.effect_names() == ["create_work_order", "set_vehicle_unavailable"]
    assert env.store.get_run(rid)["status"] == "AWAITING_APPROVAL"
    approve(env, rid, decision="reject")
    assert env.world.effect_names()[2:] == ["set_vehicle_available", "cancel_work_order"]  # reverse order
    assert env.world.vehicles["veh-1"].status == "ready"
    assert env.world.work_orders["WO-1"].status == "closed"
    assert env.store.get_run(rid)["status"] == "ESCALATED"
    assert env.store.get_escalation(rid)["reason"] == "rejected_after_writes"


def test_step_failure_compensates_in_reverse_and_escalates(env):
    env.world.fail_writes.add("assign_operator")
    rid = start(env)
    approve(env, rid)
    assert env.world.effect_names() == ["create_work_order", "set_vehicle_unavailable", "reassign_block",
                                        "reassign_block", "set_vehicle_available", "cancel_work_order"]
    assert env.world.blocks["B1"].vehicle_id == "veh-1"
    assert env.store.get_escalation(rid)["reason"].startswith("step_failed:assign_operator")
    assert env.store.get_decision(rid, 1)["outcome"] == "compensated"


def test_retried_write_single_effect(env):
    env.world.lose_response_once.add("set_vehicle_unavailable")  # applied, response lost -> retry same key
    rid = start(env)
    approve(env, rid)
    assert env.world.effect_names() == SAGA
    attempts = [c for c in env.world.calls if c[0] == "set_vehicle_unavailable"]
    assert len(attempts) == 2 and len({k for _, k in attempts}) == 1


def test_process_killed_between_steps_resumes_without_repeats(env):
    env.world.crash_before = "reassign_block"
    rid = start(env)
    with pytest.raises(SimulatedCrash):
        approve(env, rid)
    assert env.world.effect_names() == ["create_work_order", "set_vehicle_unavailable"]
    env.rebuild()  # new graph + new sqlite connection on the same file
    env.graph.invoke(None, run_config(rid))
    assert env.world.effect_names() == SAGA
    assert len(env.world.effects) == len(set(env.world.effects))
    assert env.store.get_run(rid)["status"] == "WAITING_FOR_REPAIR"
    assert len([c for c in env.world.calls if c[0] == "create_work_order"]) == 1


def test_spare_taken_after_proposal_reproposes_without_writing(env):
    rid = start(env)
    first = run_of(env, rid)["plan"]["steps"][2]["params"]["to_vehicle_id"]
    env.world.take_spare(first)
    approve(env, rid)
    assert env.world.effects == []
    r = run_of(env, rid)
    assert r["plan_version"] == 2
    assert r["plan"]["steps"][2]["params"]["to_vehicle_id"] != first
    assert env.store.get_run(rid)["status"] == "AWAITING_APPROVAL"
    assert r["plan"]["steps"][0]["idempotency_key"].endswith(":2")


def test_last_spare_is_hard_limit_escalates(env):
    env.world.take_spare("veh-3")
    env.world.take_spare("veh-4")  # only veh-2 ready, min_spares=1 -> cannot use it
    rid = start(env)
    assert env.store.get_run(rid)["status"] == "ESCALATED"
    assert env.world.effects == []
    assert any(v.startswith("last_spare") for v in env.store.get_escalation(rid)["violations"])


def test_kill_switch_mid_run_halts_writes(env):
    rid = start(env)
    orig = env.tools.writes.set_vehicle_unavailable

    def flip(*a, **k):
        r = orig(*a, **k)
        env.policy.kill_switch = True
        return r
    env.tools.writes.set_vehicle_unavailable = flip
    approve(env, rid)
    assert env.world.effect_names() == ["create_work_order", "set_vehicle_unavailable"]
    assert env.store.get_run(rid)["status"] == "ESCALATED"
    esc = env.store.get_escalation(rid)
    assert esc["reason"] == "kill_switch"
    assert esc["still_applied"] == ["create_work_order", "set_vehicle_unavailable"]


def test_kill_switch_on_at_start_is_recommend_only(make_env):
    env = make_env(policy=default_policy(kill_switch=True))
    rid = start(env)
    assert env.world.effects == [] and env.store.get_run(rid)["status"] == "ESCALATED"
    assert env.store.get_plan(rid, 1) is not None  # recommendation persisted


def test_compensation_failure_escalates_with_exact_state(env):
    env.world.fail_writes.add("publish_rider_alert")
    env.world.fail_compensations.add("assign_operator")
    rid = start(env)
    approve(env, rid)
    esc = env.store.get_escalation(rid)
    assert esc["reason"] == "compensation_failed"
    assert esc["comp_failed"]["step"] == "assign_operator:B1"
    assert esc["original_reason"].startswith("step_failed:publish_rider_alert")
    assert esc["still_applied"] == ["create_work_order", "set_vehicle_unavailable", "reassign_block:B1",
                                    "assign_operator:B1"]
    assert env.store.get_run(rid)["status"] == "ESCALATED"
    assert env.store.get_decision(rid, 1)["outcome"] == "escalated"


def test_infeasible_model_output_uses_fallback_flagged(make_env):
    bad = {"decision": "pull_now", "assignments": [{"block_id": "B1", "spare_id": "veh-99", "operator_id": "OP-1"}]}
    rs = ScriptedReasoner([bad, bad])
    env = make_env(reasoner=rs)
    rid = start(env)
    assert rs.calls == 2  # one retry
    plan = run_of(env, rid)["plan"]
    assert plan["fallback_used"] is True
    assert any("model output rejected" in a for a in plan["assumptions"])
    assert plan["steps"][2]["params"]["to_vehicle_id"] == "veh-2"  # depot match ranks first
    assert env.store.get_decision(rid, 1)["model"] == "code_ranked_fallback"


def test_model_exception_then_retry_succeeds_without_fallback(make_env):
    rs = ScriptedReasoner([RuntimeError("boom")])
    env = make_env(reasoner=rs)
    rid = start(env)
    assert rs.calls == 2 and run_of(env, rid)["plan"]["fallback_used"] is False


def test_model_cannot_pick_operator_over_hours(make_env):
    bad = {"decision": "pull_now", "assignments": [{"block_id": "B1", "spare_id": "veh-2", "operator_id": "OP-old"}]}
    env = make_env(reasoner=ScriptedReasoner([bad, bad]))
    rid = start(env)
    plan = run_of(env, rid)["plan"]
    assert plan["fallback_used"] and plan["steps"][3]["params"]["operator_id"] == "OP-1"


def test_replayed_events_create_no_new_runs(env):
    ev = fault_event()
    rid = start(env, ev)
    assert handle_event(ev, env.store, env.graph).action == "duplicate"
    r2 = handle_event(fault_event(id_="fault:2"), env.store, env.graph)  # second start event -> context
    assert (r2.action, r2.run_id) == ("attached", rid)
    assert len(env.store.list_runs()) == 1
    assert [e["id"] for e in env.store.context(rid)] == ["fault:2"]
    assert handle_event(fault_event(id_="fault:2"), env.store, env.graph).action == "duplicate"


def test_update_event_invalidates_plan_awaiting_approval(env):
    rid = start(env)
    other = make_event("VehicleStatusChanged", "vs:1", {"native_vehicle_id": "BUS-44", "status": "reserved",
                                                        "changed_by": "dispatcher", "refs": {"canonical": "veh-2"}})
    assert handle_event(other, env.store, env.graph).action == "ignored"  # no run for that vehicle
    mine = make_event("VehicleStatusChanged", "vs:2", {"native_vehicle_id": "BUS-44", "status": "reserved",
                                                       "changed_by": "dispatcher", "refs": {"canonical": "veh-1"}})
    assert handle_event(mine, env.store, env.graph).action == "signaled"
    approve(env, rid)
    assert env.world.effects == [] and run_of(env, rid)["plan_version"] == 2  # re-proposed, no write


def test_human_opened_wo_reused(env):
    env.world.work_orders["WO-H"] = WorkOrder(id="WO-H", native_no="555", asset_vehicle_id="veh-1", source="human")
    rid = start(env)
    assert "create_work_order" not in [s["action"] for s in run_of(env, rid)["plan"]["steps"]]
    approve(env, rid)
    assert env.world.effect_names() == SAGA[1:]
    assert run_of(env, rid)["wo_id"] == "WO-H"


def test_multi_day_repair_on_sim_clock_ends_on_work_order_closed(env):
    rid = start(env)
    approve(env, rid)
    env.clock.advance(hours=72)
    assert env.store.get_run(rid)["status"] == "WAITING_FOR_REPAIR"
    assert expire_approvals(env.graph, env.store, env.clock) == []  # timers never end a repair wait
    assert handle_event(wo_closed_event(), env.store, env.graph).action == "resumed"
    resume_return(env.graph, rid, "approve", "alice")
    assert env.store.get_run(rid)["status"] == "DONE"


def test_wo_closed_before_waiting_is_parked_then_applied(env):
    rid = start(env)
    assert handle_event(wo_closed_event(), env.store, env.graph).action == "parked"
    approve(env, rid)
    assert env.store.get_run(rid)["status"] == "RETURNING"


def test_approval_timeout_escalates(env):
    rid = start(env)
    env.clock.advance(minutes=14)
    assert expire_approvals(env.graph, env.store, env.clock) == []
    env.clock.advance(minutes=2)
    assert expire_approvals(env.graph, env.store, env.clock) == [rid]
    assert env.store.get_run(rid)["status"] == "ESCALATED"
    assert env.store.get_escalation(rid)["reason"] == "approval_expired"
    assert env.world.effects == []


def test_edit_is_revalidated(env):
    rid = start(env)
    approve(env, rid, decision="edit", edits={"assignments": [{"block_id": "B1", "spare_id": "veh-99"}]})
    assert env.world.effects == [] and env.store.get_run(rid)["status"] == "AWAITING_APPROVAL"
    approve(env, rid, decision="edit", edits={"assignments": [{"block_id": "B1", "spare_id": "veh-3"}]})
    assert env.world.blocks["B1"].vehicle_id == "veh-3"
    assert run_of(env, rid)["plan_version"] == 2


def test_data_unavailable_escalates_no_guess(env):
    env.world.unavailable_reads.add("list_eligible_operators")
    rid = start(env)
    assert env.store.get_run(rid)["status"] == "ESCALATED"
    assert env.store.get_escalation(rid)["reason"].startswith("unavailable:")
