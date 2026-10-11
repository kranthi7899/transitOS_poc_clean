"""Planner behaviour from the clearance module (Phase 10 Step 1): ranking, graded fit, spare reuse,
acknowledgement before execution, decision record."""
from __future__ import annotations

from datetime import timedelta

from conftest import Env  # noqa: F401  (fixtures: env, make_env)

from transitos.contracts.objects import Block, Needs, Operator, Vehicle
from transitos.workflow.fakes import T0, FakeWorld, default_world, fault_event
from transitos.workflow.graph import run_config
from transitos.workflow.starter import handle_event, resume_approval

AGENCY = "agency-a"
SAGA_REASSIGN = "reassign_block"


def world_with(vehicles, operators=None, blocks=None) -> FakeWorld:
    w = default_world()
    w.vehicles = {v.id: v for v in vehicles}
    w.operators = {o.id: o for o in (operators or [Operator(id="OP-1", qualifications=["standard", "articulated"]),
                                                   Operator(id="OP-2", qualifications=["standard", "articulated"])])}
    w.blocks = {b.id: b for b in (blocks or [block("B1", 1, 5)])}
    return w


def block(bid, start_h, end_h, op="OP-1", vtype="standard") -> Block:
    return Block(id=bid, vehicle_id="veh-1", operator_id=op, start=T0 + timedelta(hours=start_h),
                 end=T0 + timedelta(hours=end_h), start_depot="north", needs=Needs(seats=20, type=vtype))


def veh(vid, vtype="standard", status="ready", depot="north") -> Vehicle:
    return Vehicle(id=vid, agency=AGENCY, seats=40, type=vtype, status=status, depot=depot)


def run_of(env, rid):
    return env.state(rid).values["run"]


def start(env):
    r = handle_event(fault_event(), env.store, env.graph, env.clock)
    assert r.action == "started"
    return r.run_id


def approve(env, rid, **kw):
    resume_approval(env.graph, env.store, rid, "approve", "alice", **kw)


def spares_of(run):
    return [s["params"]["to_vehicle_id"] for s in run["plan"]["steps"] if s["action"] == SAGA_REASSIGN]


# ---- ranking -------------------------------------------------------------------------------------------
def test_exact_type_spare_beats_a_larger_one_even_when_listed_later(make_env):
    env = make_env(world=world_with([veh("veh-1"), veh("veh-2", "articulated"), veh("veh-3", "standard")]))
    rid = start(env)
    run = run_of(env, rid)
    assert spares_of(run) == ["veh-3"]
    assert run["plan"]["vehicle_fit"] == "exact"
    larger = [a for a in run["plan"]["alternatives"] if a.get("tier") == "larger"]
    assert larger and larger[0]["assignments"][0]["spare_id"] == "veh-2"       # kept as a runner-up, never first
    assert run["plan"]["rank_reason"].startswith("plan 1 ")


def test_runners_up_are_real_plans_capped_at_two_alternatives(make_env):
    env = make_env(world=world_with([veh("veh-1")] + [veh(f"veh-{i}") for i in range(2, 7)]))
    plan = run_of(env, start(env))["plan"]
    alts = [a for a in plan["alternatives"] if "rank" in a]
    assert [a["rank"] for a in alts] == [2, 3] and plan["rank_reason"]
    assert all(a["assignments"][0]["spare_id"] not in spares_of({"plan": plan}) for a in alts)


def test_larger_class_is_the_fallback_when_no_exact_spare_exists(make_env):
    env = make_env(world=world_with([veh("veh-1"), veh("veh-2", "articulated"), veh("veh-3", "articulated")]))
    plan = run_of(env, start(env))["plan"]
    assert plan["vehicle_fit"] == "larger"
    fit = next(i for i in plan["clearance"] if i["rule"] == "vehicle_fit")
    assert (fit["status"], fit["requires_ack"], fit["basis"]) == ("warn", True, "native")


def test_smaller_class_only_means_no_plan(make_env):
    env = make_env(world=world_with([veh("veh-1"), veh("veh-2", "cutaway"), veh("veh-3", "cutaway")]))
    rid = start(env)
    assert env.store.get_run(rid)["status"] == "ESCALATED" and env.world.effects == []


# ---- spare reuse ----------------------------------------------------------------------------------------
def test_one_spare_serves_two_non_overlapping_blocks(make_env):
    w = world_with([veh("veh-1"), veh("veh-2"), veh("veh-3")], blocks=[block("B1", 1, 3), block("B2", 5, 7, op="OP-2")])
    env = make_env(world=w)
    rid = start(env)
    assert env.store.get_run(rid)["status"] == "AWAITING_APPROVAL"
    run = run_of(env, rid)
    assert len(spares_of(run)) == 2 and len(set(spares_of(run))) == 1
    approve(env, rid)
    assert env.store.get_run(rid)["status"] == "WAITING_FOR_REPAIR"
    assert w.blocks["B1"].vehicle_id == w.blocks["B2"].vehicle_id


def test_overlapping_blocks_need_two_distinct_spares_and_the_last_spare_still_counts(make_env):
    env = make_env(world=world_with([veh("veh-1"), veh("veh-2"), veh("veh-3")],
                                    blocks=[block("B1", 1, 5), block("B2", 3, 7)]))
    rid = start(env)
    assert env.store.get_run(rid)["status"] == "ESCALATED"          # 2 ready - 2 distinct used = 0 < 1
    assert any(v.startswith("last_spare") for v in env.store.get_escalation(rid)["violations"])
    assert env.world.effects == []


def test_overlapping_blocks_with_a_spare_to_spare_get_two_distinct_spares(make_env):
    env = make_env(world=world_with([veh("veh-1"), veh("veh-2"), veh("veh-3"), veh("veh-4")],
                                    blocks=[block("B1", 1, 5), block("B2", 3, 7, op="OP-2")]))
    rid = start(env)
    run = run_of(env, rid)
    assert env.store.get_run(rid)["status"] == "AWAITING_APPROVAL" and len(set(spares_of(run))) == 2


# ---- acknowledgement ------------------------------------------------------------------------------------
def larger_only_env(make_env):
    return make_env(world=world_with([veh("veh-1"), veh("veh-2", "articulated"), veh("veh-3", "articulated")]))


def test_execution_does_not_start_until_requires_ack_items_are_acknowledged(make_env):
    env = larger_only_env(make_env)
    rid = start(env)
    approve(env, rid)                                                # approver forgets to acknowledge
    assert env.world.effects == [] and env.store.get_run(rid)["status"] == "AWAITING_APPROVAL"
    run = run_of(env, rid)
    assert run["decision"] is None and run["acknowledged"] == []
    approve(env, rid, acknowledged=["vehicle_fit"])
    assert env.world.effects and env.store.get_run(rid)["status"] == "WAITING_FOR_REPAIR"
    assert run_of(env, rid)["acknowledged"] == ["vehicle_fit"]


def test_act_autonomy_does_not_bypass_acknowledgement(make_env):
    from transitos.workflow.fakes import default_policy
    pol = default_policy()
    pol.autonomy = {k: "act" for k in pol.autonomy}
    env = make_env(world=world_with([veh("veh-1"), veh("veh-2", "articulated"), veh("veh-3", "articulated")]),
                   policy=pol)
    rid = start(env)
    assert env.world.effects == [] and env.store.get_run(rid)["status"] == "AWAITING_APPROVAL"


def test_decision_record_carries_clearance_acknowledged_and_agency_fit_setting(make_env):
    env = larger_only_env(make_env)
    rid = start(env)
    approve(env, rid, acknowledged=["vehicle_fit"])
    rec = env.store.get_decision(rid, 1)
    assert rec["vehicle_fit"] == "allow_larger" and rec["acknowledged"] == ["vehicle_fit"]
    assert any(c["rule"] == "vehicle_fit" and c["status"] == "warn" for c in rec["clearance"])
    assert all(not (c["status"] == "pass" and c["basis"] == "native") for c in rec["clearance"])


def test_a_new_plan_after_a_stale_spare_needs_fresh_acknowledgement(make_env):
    env = make_env(world=world_with([veh("veh-1"), veh("veh-2", "articulated"), veh("veh-3", "articulated"),
                                     veh("veh-4", "articulated")]))
    rid = start(env)
    first = spares_of(run_of(env, rid))[0]
    env.world.take_spare(first)
    approve(env, rid, acknowledged=["vehicle_fit"])                  # plan v1 is stale: re-planned as v2
    run = run_of(env, rid)
    assert run["plan_version"] == 2 and env.world.effects == [] and run["acknowledged"] == []
    approve(env, rid, acknowledged=["vehicle_fit"])
    assert env.world.effects


def test_human_edit_to_a_larger_spare_is_cleared_and_flagged(make_env):
    env = make_env(world=world_with([veh("veh-1"), veh("veh-2"), veh("veh-3", "articulated")]))
    rid = start(env)
    assert run_of(env, rid)["plan"]["vehicle_fit"] == "exact"
    resume_approval(env.graph, env.store, rid, "edit", "alice",
                    edits={"assignments": [{"block_id": "B1", "spare_id": "veh-3"}]})
    run = run_of(env, rid)
    assert run["plan_version"] == 2 and run["plan"]["vehicle_fit"] == "larger"
    assert env.world.effects == [] and env.store.get_run(rid)["status"] == "AWAITING_APPROVAL"


def test_interrupt_nodes_still_write_nothing_with_acknowledgement_pending(make_env):
    env = larger_only_env(make_env)
    rid = start(env)
    env.graph.invoke(None, run_config(rid))
    assert env.world.calls == []
