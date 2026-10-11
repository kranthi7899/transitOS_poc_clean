"""F3 on the REAL mock products: a blank cell or an absent key becomes `unknown_fields` on THAT record (contract
v1.2, C-2), never a default value; the scheduling capability profile is computed from the payload."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from .conftest import AG, H
from .test_clearance_vs_mocks import make_env

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")
T7, T9 = datetime(2026, 1, 5, 7, 0, tzinfo=UTC), datetime(2026, 1, 5, 9, 0, tzinfo=UTC)


@pytest.fixture
def env(mocks):
    return make_env(mocks)


def affected(env, vehicle="veh-1"):
    return env.tools.list_affected_blocks(AG, vehicle, T7, T9).value[0]


def operators_by_id(env, block):
    return {o.id: o for o in env.tools.list_eligible_operators(AG, block).value}


def test_blank_workforce_cells_are_unknown_for_that_operator_only(env):
    r = env.c("workforce").post("/sim/operator-fields", headers=H, json={
        "blank": ["hours_today", "lift_trained", "available_from", "medical_valid_to"], "blank_for": ["OP-311"]})
    assert r.status_code == 200
    ops = operators_by_id(env, affected(env))
    assert {"hours_today", "lift_trained", "available_from", "medical_valid_to"} <= set(ops["op-2"].unknown_fields)
    assert not {"hours_today", "lift_trained", "available_from"} & set(ops["op-1"].unknown_fields)
    prof = env.tools.capability_profiles(AG).value["workforce"]["fields"]
    assert prof["hours_today"] == "native"                       # the column is there; the CELL is blank


def test_a_blank_lift_trained_cell_keeps_the_operator_listed_as_unverifiable_not_excluded(env):
    t5 = datetime(2026, 1, 5, 5, 0, tzinfo=UTC)
    block = next(b for b in env.tools.list_blocks(AG, t5, T7).value if b.needs.lift)   # 05:00-07:00 lift blocks
    assert "op-4" not in operators_by_id(env, block)               # OP-313 is known NOT lift-trained: excluded
    env.c("workforce").post("/sim/operator-fields", headers=H, json={"blank": ["lift_trained"],
                                                                      "blank_for": ["OP-313"]})
    assert "op-4" in operators_by_id(env, block)                   # unknown, not a violation


def test_blank_hours_cell_is_not_zero_hours(env):
    env.c("workforce").post("/sim/operator-fields", headers=H, json={"blank": ["hours_today", "hours_week"]})
    op = operators_by_id(env, affected(env))["op-1"]
    assert {"hours_today", "hours_week"} <= set(op.unknown_fields)


def test_dropped_workforce_columns_are_listed_per_record_too(env):
    env.c("workforce").post("/sim/operator-fields", headers=H, json={"drop": ["licence_class"]})
    op = operators_by_id(env, affected(env))["op-1"]
    assert "licence_class" in op.unknown_fields


def test_blank_scheduling_cells_are_unknown_for_that_vehicle_only(env):
    r = env.c("scheduling").post("/sim/fields", headers=H, json={
        "vehicles": {"blank": ["lift_ok", "type", "wheelchair_positions", "seats"]}, "ids": ["BUS-53"]})
    assert r.status_code == 200
    v10, v12 = env.tools.get_vehicle(AG, "veh-10").value, env.tools.get_vehicle(AG, "veh-12").value
    assert {"lift_ok", "type", "wheelchair_positions", "seats"} <= set(v10.unknown_fields)
    assert v12.unknown_fields == []


def test_scheduling_capability_profile_is_computed_from_the_payload(env):
    prof = env.tools.capability_profiles(AG).value["scheduling"]["fields"]
    assert prof["seats"] == "native" and prof["lift_ok"] == "native" and prof["length_ft"] == "native"
    env.c("scheduling").post("/sim/fields", headers=H, json={"vehicles": {"drop": ["lift_ok", "length_ft"]}})
    prof = env.tools.capability_profiles(AG).value["scheduling"]["fields"]
    assert prof["lift_ok"] == "unknown" and prof["length_ft"] == "unknown" and prof["seats"] == "native"
    v = env.tools.get_vehicle(AG, "veh-10").value
    assert {"lift_ok", "length_ft"} <= set(v.unknown_fields)


def test_undeclared_block_needs_are_unknown_not_no_requirement(env):
    b = affected(env)
    assert b.needs.unknown_fields == []                            # every need declared (length limit: none)
    env.c("scheduling").post("/sim/fields", headers=H, json={"needs": {"drop": ["lift", "type"],
                                                                        "blank": ["seats"]}})
    b = affected(env)
    assert {"lift", "type", "seats"} <= set(b.needs.unknown_fields)


def test_blank_cells_leave_the_product_state_untouched(env):
    """The switch only changes what the product RENDERS (a blank cell in an export); writes still validate."""
    env.c("scheduling").post("/sim/fields", headers=H, json={"vehicles": {"blank": ["seats"]},
                                                            "needs": {"blank": ["seats"]}})
    env.c("scheduling").post("/sim/fields", headers=H, json={})       # restore
    assert env.tools.get_vehicle(AG, "veh-10").value.unknown_fields == []
