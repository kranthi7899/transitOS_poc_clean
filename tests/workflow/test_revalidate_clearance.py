"""F1: revalidation re-judges the APPROVED plan's clearance against fresh facts. A requires_ack item that appears
between proposal and execution never runs unacknowledged: before any write the run goes back to approval, after
writes have started it is compensated and escalated. execute_step itself refuses an unacknowledged item."""
from __future__ import annotations

from datetime import timedelta

import pytest

from transitos.clearance.facts import native_profile
from transitos.contracts.clearance import ClearanceItem
from transitos.contracts.objects import Block, Needs, Operator, Vehicle
from transitos.contracts.results import Result
from transitos.workflow.fakes import (
    T0,
    SimulatedCrash,
    default_policy,
    default_world,
    fault_event,
)
from transitos.workflow.graph import run_config
from transitos.workflow.starter import handle_event, resume_approval

AGENCY = "agency-a"
SAGA = ["create_work_order", "set_vehicle_unavailable", "reassign_block", "publish_rider_alert"]


def world():
    w = default_world()
    w.vehicles = {v.id: v for v in (
        Vehicle(id="veh-1", agency=AGENCY, seats=40, type="standard", status="in_service", depot="north"),
        Vehicle(id="veh-2", agency=AGENCY, seats=40, type="standard", status="ready", depot="north"),
        Vehicle(id="veh-3", agency=AGENCY, seats=40, type="standard", status="ready", depot="north"))}
    w.blocks = {"B1": Block(id="B1", vehicle_id="veh-1", operator_id="OP-1", start=T0 + timedelta(hours=1),
                            end=T0 + timedelta(hours=5), start_depot="north", needs=Needs(seats=20, type="standard"))}
    med = T0 + timedelta(days=365)
    w.operators = {o: Operator(id=o, qualifications=["standard"], hours_today=2.0, medical_valid_to=med)
                   for o in ("OP-1", "OP-2")}
    return w


def lose_hours_and_availability(env):
    """The reviewer's probe: after the proposal the workforce product stops supplying weekly hours and
    availability (the profile loses them)."""
    drop = ("licence_class", "endorsements", "trained_vehicle_types", "training_valid_to", "duty_times",
            "hours_week", "availability")
    env.tools.reads.capability_profiles = lambda agency: Result.success(
        {"scheduling": native_profile("scheduling").to_dict(),
         "workforce": native_profile("workforce", drop=drop).to_dict()})


def start(env):
    r = handle_event(fault_event(), env.store, env.graph, env.clock)
    assert r.action == "started"
    return r.run_id


def run_of(env, rid):
    return env.state(rid).values["run"]


def ack_rules(run):
    return sorted({i["rule"] for i in run["plan"]["clearance"] if i["requires_ack"]})


def test_data_lost_after_approval_before_any_write_sends_the_run_back_to_approval(make_env):
    env = make_env(world=world())
    rid = start(env)
    assert ack_rules(run_of(env, rid)) == []                        # the proposed plan needs no acknowledgement
    lose_hours_and_availability(env)
    resume_approval(env.graph, env.store, rid, "approve", "alice", acknowledged=[])
    assert env.world.effects == []                                  # nothing ran
    run = run_of(env, rid)
    assert env.store.get_run(rid)["status"] == "AWAITING_APPROVAL"
    assert ack_rules(run) == ["operator_availability", "operator_hours_week"]   # the approver now sees them
    assert run["acknowledged"] == [] and run["decision"] is None
    resume_approval(env.graph, env.store, rid, "approve", "alice", acknowledged=[])   # still not acknowledged
    assert env.world.effects == []
    resume_approval(env.graph, env.store, rid, "approve", "alice",
                    acknowledged=["operator_availability", "operator_hours_week"])
    assert env.world.effect_names() == SAGA


def test_approval_carrying_stale_acknowledgements_is_reset_when_new_items_appear(make_env):
    """A new warn/unverifiable item resets acknowledgements so the approver looks at the new list."""
    env = make_env(world=world())
    rid = start(env)
    lose_hours_and_availability(env)
    resume_approval(env.graph, env.store, rid, "approve", "alice", acknowledged=["operator_hours_week"])
    run = run_of(env, rid)
    assert env.world.effects == [] and run["acknowledged"] == []
    rec = env.store.get_decision(rid, run["plan_version"])
    assert {c["rule"] for c in rec["clearance"] if c["status"] == "unverifiable"} >= {
        "operator_availability", "operator_hours_week"}


def test_data_lost_mid_saga_is_compensated_and_escalated(make_env):
    pol = default_policy()
    pol.autonomy["create_work_order"] = "act"
    pol.autonomy["set_vehicle_unavailable"] = "act"
    env = make_env(world=world(), policy=pol)
    rid = start(env)
    assert env.world.effect_names() == ["create_work_order", "set_vehicle_unavailable"]
    assert env.store.get_run(rid)["status"] == "AWAITING_APPROVAL"   # reassign_block needs approval
    lose_hours_and_availability(env)
    resume_approval(env.graph, env.store, rid, "approve", "alice", acknowledged=[])
    names = env.world.effect_names()
    assert "reassign_block" not in names and "publish_rider_alert" not in names
    assert names[2:] == ["set_vehicle_available", "cancel_work_order"]   # undone in reverse
    esc = env.store.get_escalation(rid)
    assert env.store.get_run(rid)["status"] == "ESCALATED"
    assert esc["reason"] == "stale_after_writes" and esc["still_applied"] == []
    assert any("unacknowledged" in r and "operator_hours_week" in r for r in esc["stale_reasons"])


def test_execute_step_itself_refuses_an_unacknowledged_requires_ack_item(make_env):
    """Defence in depth: whatever routed the run to execute_step, an unacknowledged requires_ack item in the
    plan stops it, also after writes have started (it compensates then)."""
    env = make_env(world=world())
    env.world.crash_before = "reassign_block"
    rid = start(env)
    with pytest.raises(SimulatedCrash):
        resume_approval(env.graph, env.store, rid, "approve", "alice")
    assert env.world.effect_names() == ["create_work_order", "set_vehicle_unavailable"]
    cfg = run_config(rid)
    st = env.graph.get_state(cfg)
    assert st.next == ("execute_step",)
    run = dict(st.values["run"])
    run["plan"] = dict(run["plan"])
    run["plan"]["clearance"] = run["plan"]["clearance"] + [ClearanceItem(
        rule="operator_medical", status="unverifiable", basis="unknown", requires_ack=True).model_dump()]
    env.graph.update_state(cfg, {"run": run})
    env.graph.invoke(None, cfg)
    names = env.world.effect_names()
    assert "reassign_block" not in names and names[2:] == ["set_vehicle_available", "cancel_work_order"]
    esc = env.store.get_escalation(rid)
    assert esc["reason"] == "unacknowledged:operator_medical"
