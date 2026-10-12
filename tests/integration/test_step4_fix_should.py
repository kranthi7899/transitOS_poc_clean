"""Step 4 fix round SHOULD-DO. F4: with OPEN pending-repair requests on TWO vehicles, each vehicle's open work order
is its own request (kills mutant m8: the request lookup ignoring the asset). F5: the repair_with_parts_wait ROW checks
that the parts wait actually happened (kills mutant m6: the foreman never waiting for parts)."""
from __future__ import annotations

import dataclasses

import pytest

from transitos.integration.world import make_world
from transitos.mocks.crosswalk_seed import crosswalk_entries
from transitos.tools.common import StaticCrosswalk
from transitos.tools.idem_store import IdemStore
from transitos.tools.policy import KillSwitchStore
from transitos.tools.registry import Registry
from transitos.verification.runner import run_scenario
from transitos.verification.scenarios import BY_ID

from .conftest import AG, H


def toolset(mocks):
    xw = StaticCrosswalk()
    for e in crosswalk_entries(AG):
        xw.add(AG, e.canonical_id, **{e.product: e.native_id})
    return Registry(clients={n: mocks.c(n) for n in ("maintenance", "scheduling", "workforce", "alerts")},
                    crosswalk=xw, idem=IdemStore(), killswitch=KillSwitchStore(), sleep=lambda s: None,
                    settings_patch={AG: {"maintenance": {"mode": "pending_repair"}}}).toolset(AG)


@pytest.mark.parametrize("order", [("1106", "1107"), ("1107", "1106")])
def test_each_vehicle_gets_its_own_open_request(mocks, order):
    m = mocks.c("maintenance")
    ids = {a: m.post("/pending-repairs", json={"asset": a, "requested_by": "system"},
                     headers={**H, "Idempotency-Key": f"k-{a}"}).json()["id"] for a in order}
    ts = toolset(mocks)
    for vid, asset in (("veh-1", "1106"), ("veh-2", "1107")):
        r = ts.find_open_work_order(AG, vid)
        assert r.status == "ok" and r.value is not None
        assert (r.value.id, r.value.asset_vehicle_id) == (ids[asset], vid)
    assert ts.find_open_work_order(AG, "veh-3").value is None          # no request on veh-3


def test_the_parts_wait_row_lists_the_shop_statuses_it_expects():
    assert BY_ID["repair_with_parts_wait"].expect_shop_statuses == ("IN_PROGRESS", "WAITING_PARTS", "IN_PROGRESS")


def test_the_parts_wait_row_fails_when_no_parts_wait_happens():
    sc = BY_ID["repair_with_parts_wait"]
    no_wait = dataclasses.replace(sc, tweaks=dataclasses.replace(
        sc.tweaks, foreman=dataclasses.replace(sc.tweaks.foreman, parts_wait_prob=0.0)))
    r = run_scenario(make_world(), no_wait)
    assert not r.passed and any("WAITING_PARTS" in m for m in r.mismatches), r.mismatches


def test_the_parts_wait_row_passes_and_observes_the_trail():
    r = run_scenario(make_world(), BY_ID["repair_with_parts_wait"], keep_observation=True)
    assert r.passed, r.mismatches
    done = [(e["status"], e["at"]) for e in r.observation.shop_log if e["job"] == "status" and e["skipped"] is None]
    assert done == [("IN_PROGRESS", "2026-10-10T09:51:00+00:00"), ("WAITING_PARTS", "2026-10-10T09:51:00+00:00"),
                    ("IN_PROGRESS", "2026-10-10T12:51:00+00:00")]
