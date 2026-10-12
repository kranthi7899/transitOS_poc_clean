"""Owner decision Q1: a limit a block declares (max_vehicle_length_ft) is checked only against a value the product
PROVIDED. Over the limit: excluded. Not provided: an unverifiable item (requires acknowledgement, names the missing
field), ranked after every verified option, still offered even when it is the only option. An agency default or a
'typical for the type' value never excludes anything."""
from __future__ import annotations

from datetime import timedelta

from test_clearance_plan import approve, run_of, spares_of, start, veh, world_with

from transitos.contracts.objects import Block, Needs
from transitos.workflow.fakes import T0


def limited_block(limit: float, vtype="standard") -> Block:
    return Block(id="B1", vehicle_id="veh-1", operator_id="OP-1", start=T0 + timedelta(hours=1),
                 end=T0 + timedelta(hours=5), start_depot="north",
                 needs=Needs(seats=20, type=vtype, max_vehicle_length_ft=limit))


def with_length(v, length):
    return v.model_copy(update={"length_ft": length})


def test_a_provided_length_over_the_limit_is_excluded(make_env):
    long, ok = with_length(veh("veh-2"), 60.0), with_length(veh("veh-3"), 40.0)
    env = make_env(world=world_with([veh("veh-1"), long, ok, with_length(veh("veh-4"), 40.0)],
                                    blocks=[limited_block(45.0)]))
    rid = start(env)
    assert env.store.get_run(rid)["status"] == "AWAITING_APPROVAL"
    assert "veh-2" not in spares_of(run_of(env, rid))


def test_only_over_limit_vehicles_means_no_plan(make_env):
    env = make_env(world=world_with([veh("veh-1"), with_length(veh("veh-2"), 60.0), with_length(veh("veh-3"), 60.0)],
                                    blocks=[limited_block(45.0)]))
    rid = start(env)
    assert env.store.get_run(rid)["status"] == "ESCALATED" and env.world.effects == []


def test_a_vehicle_with_no_provided_length_ranks_after_a_verified_one_even_when_listed_first(make_env):
    unknown, verified = veh("veh-2"), with_length(veh("veh-3"), 40.0)
    env = make_env(world=world_with([veh("veh-1"), unknown, verified, with_length(veh("veh-4"), 40.0)],
                                    blocks=[limited_block(45.0)]))
    run = run_of(env, start(env))
    assert spares_of(run) == ["veh-3"]
    assert not any(i["rule"] == "vehicle_length" and i["status"] != "pass" for i in run["plan"]["clearance"])


def test_a_vehicle_with_no_provided_length_is_still_offered_when_it_is_the_only_option(make_env):
    env = make_env(world=world_with([veh("veh-1"), veh("veh-2"), veh("veh-3")], blocks=[limited_block(45.0)]))
    rid = start(env)
    assert env.store.get_run(rid)["status"] == "AWAITING_APPROVAL"
    item = next(i for i in run_of(env, rid)["plan"]["clearance"] if i["rule"] == "vehicle_length")
    assert (item["status"], item["basis"], item["requires_ack"]) == ("unverifiable", "unknown", True)
    assert "length_ft" in item["detail"]            # the acknowledgement names the missing field
    approve(env, rid)                                # no acknowledgement given: nothing executes
    assert env.world.effects == []
    approve(env, rid, acknowledged=["vehicle_length"])
    assert env.world.effects != []


def test_a_typical_value_for_the_class_never_excludes(make_env):
    # a standard bus is typically 40 ft; the block limit is 30 ft; the product did not provide a length
    env = make_env(world=world_with([veh("veh-1"), veh("veh-2"), veh("veh-3")], blocks=[limited_block(30.0)]))
    rid = start(env)
    assert env.store.get_run(rid)["status"] == "AWAITING_APPROVAL" and spares_of(run_of(env, rid))
