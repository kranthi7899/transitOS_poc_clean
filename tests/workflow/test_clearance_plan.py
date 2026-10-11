"""Planner behaviour from the clearance module (Phase 10 Step 1): ranking, graded fit, spare reuse,
acknowledgement before execution, decision record."""
from __future__ import annotations

from datetime import timedelta

from transitos.contracts.objects import Block, Needs, Operator, Vehicle
from transitos.workflow.fakes import T0, FakeWorld, default_world, fault_event
from transitos.workflow.graph import run_config
from transitos.workflow.starter import handle_event, resume_approval

AGENCY = "agency-a"
SAGA_REASSIGN = "reassign_block"


def world_with(vehicles, operators=None, blocks=None) -> FakeWorld:
    w = default_world()
    w.vehicles = {v.id: v for v in vehicles}
    med = T0 + timedelta(days=365)
    w.operators = {o.id: o for o in (operators or [
        Operator(id="OP-1", qualifications=["standard", "articulated"], medical_valid_to=med),
        Operator(id="OP-2", qualifications=["standard", "articulated"], medical_valid_to=med)])}
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


def test_an_edit_is_a_new_plan_version_and_clears_earlier_acknowledgements(make_env):
    """F10 / A74: a re-plan (new plan_version) clears acknowledgements, an edit included. The approver
    acknowledged vehicle_fit for plan v1 together with an edit; the edited plan v2 is a different plan whose
    vehicle_fit item has not been acknowledged yet, so nothing may execute."""
    env = make_env(world=world_with([veh("veh-1"), veh("veh-2", "articulated"), veh("veh-3", "articulated"),
                                     veh("veh-4", "articulated")]))
    rid = start(env)
    first = spares_of(run_of(env, rid))[0]
    other = next(v for v in ("veh-2", "veh-3", "veh-4") if v != first)
    resume_approval(env.graph, env.store, rid, "edit", "alice", acknowledged=["vehicle_fit"],
                    edits={"assignments": [{"block_id": "B1", "spare_id": other}]})
    run = run_of(env, rid)
    assert run["plan_version"] == 2 and run["acknowledged"] == []
    assert env.world.effects == [] and env.store.get_run(rid)["status"] == "AWAITING_APPROVAL"
    approve(env, rid, acknowledged=["vehicle_fit"])
    assert env.world.effects and spares_of(run_of(env, rid)) == [other]


def test_interrupt_nodes_still_write_nothing_with_acknowledgement_pending(make_env):
    env = larger_only_env(make_env)
    rid = start(env)
    env.graph.invoke(None, run_config(rid))
    assert env.world.calls == []


# ---- F6: the last-spare pre-check uses the exact minimum and the LIVE policy ------------------------------
def test_last_spare_precheck_uses_the_live_policy_not_a_startup_snapshot(make_env):
    """F6: Deps.limits is a HardLimits snapshot taken at start-up; the agency policy is read live. Raising
    min_spares after start-up must be seen by the pre-check (one source for min_spares)."""
    from transitos.contracts.policy import Limits
    from transitos.workflow.fakes import default_policy
    env = make_env(world=world_with([veh("veh-1"), veh("veh-2"), veh("veh-3"), veh("veh-4")]))
    env.policy = default_policy(limits=Limits(min_spares=3))      # 3 ready - 1 used = 2 < 3
    rid = start(env)
    esc = env.store.get_escalation(rid)
    assert any(v.startswith("last_spare:3_ready_for_1_") for v in esc["violations"]), esc["violations"]
    assert env.world.effects == []


def test_last_spare_precheck_counts_the_exact_minimum_of_distinct_spares(make_env):
    """F6: B1 can take veh-2 or veh-3, B2 (needs a lift) only veh-3; they do not overlap, so veh-3 serves both:
    1 distinct spare, 2 ready, min_spares 1 is met. A greedy count says 2 and wrongly escalates."""
    w = world_with([veh("veh-1"), veh("veh-2"), Vehicle(id="veh-3", agency=AGENCY, seats=40, type="standard",
                                                        status="ready", depot="north", lift_ok=True)],
                   blocks=[block("B1", 1, 3), Block(id="B2", vehicle_id="veh-1", operator_id="OP-2",
                                                    start=T0 + timedelta(hours=5), end=T0 + timedelta(hours=7),
                                                    start_depot="north",
                                                    needs=Needs(seats=20, type="standard", lift=True))])
    for o in w.operators.values():
        o.lift_trained = True
    env = make_env(world=w)
    rid = start(env)
    assert env.store.get_run(rid)["status"] == "AWAITING_APPROVAL", env.store.get_escalation(rid)
    assert spares_of(run_of(env, rid)) == ["veh-3", "veh-3"]


# ---- F4: one malformed record fails closed for THAT candidate; the run never hangs -------------------------
def naive_window(o: Operator) -> Operator:
    """The reviewer's probe: an availability bound without a zone (comparing it raises inside clearance)."""
    return o.model_copy(update={"available_from": (T0 - timedelta(hours=1)).replace(tzinfo=None)})


def test_a_malformed_operator_record_on_every_candidate_escalates_cleanly_with_a_decision_record(make_env):
    w = world_with([veh("veh-1"), veh("veh-2"), veh("veh-3")])
    w.operators = {k: naive_window(o) for k, o in w.operators.items()}
    env = make_env(world=w)
    rid = start(env)
    run = env.store.get_run(rid)
    assert run["status"] == "ESCALATED" and not run["active"]           # released: the vehicle is not blocked
    esc = env.store.get_escalation(rid)
    assert "no_feasible_operator:B1" in esc["violations"]
    assert any(e["rule"] == "clearance_error" and "OP-1" in e["detail"] for e in esc["clearance_errors"])
    rec = env.store.get_decision(rid, 1)
    assert rec["outcome"] == "escalated"
    assert any(c["rule"] == "clearance_error" for c in rec["clearance"])
    assert env.world.effects == []
    again = handle_event(fault_event(id_="fault:2"), env.store, env.graph, env.clock)
    assert again.action == "started" and again.run_id != rid           # a new fault starts a new run


def test_a_malformed_record_excludes_only_that_candidate_and_is_shown(make_env):
    w = world_with([veh("veh-1"), veh("veh-2"), veh("veh-3")])
    w.blocks["B1"].operator_id = "OP-old"
    w.operators["OP-old"] = Operator(id="OP-old", qualifications=["standard"], hours_today=9.5,
                                     medical_valid_to=T0 + timedelta(days=365))
    w.operators["OP-1"] = naive_window(w.operators["OP-1"])
    env = make_env(world=w)
    rid = start(env)
    run = run_of(env, rid)
    assert env.store.get_run(rid)["status"] == "AWAITING_APPROVAL"
    ops = [s["params"]["operator_id"] for s in run["plan"]["steps"] if s["action"] == "assign_operator"]
    assert ops == ["OP-2"]
    rec = env.store.get_decision(rid, 1)
    assert any(c["rule"] == "clearance_error" and "OP-1" in c["detail"] for c in rec["clearance"])
