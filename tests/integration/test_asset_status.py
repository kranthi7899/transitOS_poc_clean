"""S2 fix round F4 (Review Focus 5) through the REAL mocks, adapters and workflow: a bus READY in scheduling but
RETIRED in maintenance is never a spare. Written from the ruling before the code:
  - optional ToolSet read asset_statuses(agency, vehicle_ids) from the maintenance product via the crosswalk:
    active / retired as the product says; no maintenance asset -> missing; maintenance unreachable -> unreachable;
  - clearance rule asset_active: retired = violation, unknown = unverifiable (ack), active = pass;
  - applied at planning, in validation and at the write-time re-check of reassign_block."""
from __future__ import annotations

from dataclasses import replace

import pytest

from transitos.integration.world import IntegrationWorld
from transitos.verification.runner import run_scenario
from transitos.verification.scenarios import AGENCY, BY_ID, Outcome


@pytest.fixture
def world():
    w = IntegrationWorld()
    yield w
    if w.system is not None:
        w.system.close()


def test_asset_statuses_read_from_maintenance_via_the_crosswalk(world):
    world.apply(BY_ID["retired_spare_never_proposed"])
    got = world.system.toolset.asset_statuses(AGENCY, ["veh-1", "veh-2", "veh-3", "veh-404"])
    assert got.status == "ok"
    assert got.value == {"veh-1": "active", "veh-2": "retired", "veh-3": "active", "veh-404": "missing"}


def test_asset_statuses_when_maintenance_is_unreachable(world):
    world.apply(BY_ID["retired_spare_never_proposed"])
    world.mocks.chaos["maintenance"].set("timeout")
    got = world.system.toolset.asset_statuses(AGENCY, ["veh-2", "veh-3"])
    assert got.status == "ok" and got.value == {"veh-2": "unreachable", "veh-3": "unreachable"}


def test_write_time_recheck_blocks_a_retired_spare(world):
    world.apply(BY_ID["retired_spare_never_proposed"])
    res = world.system.toolset.reassign_block(AGENCY, "blk-1", "veh-2", "k-retired")
    assert res.is_blocked and "asset_retired" in (res.note or ""), res
    blocks = {b["id"]: b["vehicle"] for b in world._docs("scheduling", "blocks")}
    assert blocks["blk-1"] == "BUS-44"                        # nothing moved


def test_retired_spare_is_never_proposed_and_the_plan_uses_the_active_one(world):
    res = run_scenario(world, BY_ID["retired_spare_never_proposed"], keep_observation=True)
    assert res.passed, (res.mismatches, {k: v for k, v in res.invariants.items() if v})
    obs = res.observation
    assert not any("veh-2" in str(w.params) for w in obs.writes)
    run = obs.runs[0]
    spares = {s.params["to_vehicle_id"] for s in run.plan.steps if s.action == "reassign_block"}
    assert spares and "veh-2" not in spares


def test_retired_spare_as_the_only_alternative_escalates(world):
    sc = BY_ID["retired_spare_never_proposed"]
    only = replace(sc, id="retired_only_spare", tweaks=replace(sc.tweaks, spares=1, retired_spare=2),
                   expected=Outcome.ESCALATE_NO_SAFE_OPTION, script=(("deliver", 0),), expect_wo_creates=0)
    res = run_scenario(world, only, keep_observation=True)
    assert res.outcome == Outcome.ESCALATE_NO_SAFE_OPTION.value and res.observation.writes == []


def test_r4_2_write_time_ready_count_excludes_a_known_retired_spare(world):
    """R4-2: two ready spares in scheduling, veh-2 retired in maintenance; moving the block onto veh-3 would leave
    no real spare (min_spares 1). Before the fix the write re-check counted veh-2 and let it through."""
    sc = BY_ID["retired_spare_never_proposed"]
    world.apply(replace(sc, tweaks=replace(sc.tweaks, spares=2, retired_spare=2)))
    res = world.system.toolset.reassign_block(AGENCY, "blk-1", "veh-3", "k-r42")
    assert res.is_blocked and "min_spares" in (res.note or ""), res
