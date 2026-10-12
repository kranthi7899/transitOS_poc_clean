"""Bug sweep A3, J1 (A171-A172) through the REAL mocks and System. Written from the ruling before the code:
  - a work order is labelled with the vehicle that held its asset id WHEN THE ORDER WAS OPENED (a pending repair:
    when the request was created), never with who holds the id now;
  - find_open_work_order(vehicle) keeps only orders labelled with that vehicle (so does create_work_order's
    'already_open', which reuses it);
  - an order whose opened time is missing or unreadable has an UNKNOWN label: the read is refused (unavailable),
    never labelled 'now'.
Setting: asset 1106 is veh-1's until S (an hour after the start) and veh-2's from S on."""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from transitos.contracts.crosswalk import CrosswalkEntry
from transitos.integration.world import IntegrationWorld
from transitos.verification.scenarios import AGENCY, BY_ID

H = {"X-Agency": AGENCY}


@pytest.fixture
def world():
    w = IntegrationWorld()
    yield w
    if w.system is not None:
        w.system.close()


def _row(native: str, canon: str, **kw) -> CrosswalkEntry:
    return CrosswalkEntry(agency=AGENCY, product="maintenance", native_id=native, entity="asset", canonical_id=canon,
                          **kw)


def _swap_1106(world, pending: bool = False):
    sc = BY_ID["retired_spare_never_proposed"]
    world.apply(replace(sc, tweaks=replace(sc.tweaks, spares=2, retired_spare=None)))
    s = world.system
    if pending:
        s.registry_settings[AGENCY]["maintenance"] = {"mode": "pending_repair"}
    swap = s.clock.now() + timedelta(hours=1)
    s.xw.resolve(_row("1106", "veh-1", valid_to=swap))
    s.xw.resolve(_row("1106", "veh-2", valid_from=swap))
    s.xw.resolve(_row("1107", "veh-2", valid_to=swap))
    return s


def _open_wo(world) -> str:
    r = world.mocks.clients["maintenance"].post("/workorders", headers=H,
                                                json={"asset": "1106", "description": "noise", "opened_by": "human"})
    assert r.status_code < 300, r.text
    return str(r.json()["id"])


def test_j1_after_the_swap_the_new_holder_never_gets_the_old_holders_order(world):
    s = _swap_1106(world)
    old = _open_wo(world)                                                    # veh-1's, opened before the swap
    before = s.toolset.find_open_work_order(AGENCY, "veh-1")
    assert before.status == "ok" and before.value is not None and before.value.id == old
    assert before.value.asset_vehicle_id == "veh-1"
    s.clock.advance(hours=2)                                                 # after the swap, still open
    got = s.toolset.find_open_work_order(AGENCY, "veh-2")
    assert got.status == "ok" and got.value is None, got                     # NOT veh-1's order
    wo = s.toolset.get_work_order(AGENCY, old)
    assert wo.status == "ok" and wo.value.asset_vehicle_id == "veh-1", wo   # still veh-1's until it closes


def test_j1_an_order_the_new_holder_opens_after_the_swap_is_its_own(world):
    s = _swap_1106(world)
    old = _open_wo(world)
    s.clock.advance(hours=2)
    new = _open_wo(world)                                                    # veh-2's, opened after the swap
    got = s.toolset.find_open_work_order(AGENCY, "veh-2")
    assert got.status == "ok" and got.value is not None and got.value.id == new != old, got
    assert got.value.asset_vehicle_id == "veh-2"


def test_j1_already_open_never_attaches_the_new_holder_to_the_old_order(world):
    s = _swap_1106(world)
    old = _open_wo(world)
    s.clock.advance(hours=2)
    res = s.toolset.create_work_order(AGENCY, "veh-2", "fault", "k-j1")
    assert res.status == "ok" and res.value is not None and res.value.id != old, res
    assert res.note != "already_open" and res.value.asset_vehicle_id == "veh-2"


def test_j1_an_order_without_an_opened_time_is_refused_never_labelled_now(world):
    s = _swap_1106(world)
    old = _open_wo(world)
    store = world.mocks.apps["maintenance"].state.ctx.store
    doc = store.get(AGENCY, "wo", old)
    store.put(AGENCY, "wo", old, {**doc, "opened_at": None})
    got = s.toolset.find_open_work_order(AGENCY, "veh-1")
    assert got.status == "unavailable", got
    assert s.toolset.get_work_order(AGENCY, old).status == "unavailable"


def test_j1_pending_repair_is_labelled_at_its_request_time(world):
    s = _swap_1106(world, pending=True)
    r = world.mocks.clients["maintenance"].post("/pending-repairs", headers=H,
                                                json={"asset": "1106", "description": "noise", "requested_by": "human"})
    assert r.status_code < 300, r.text
    pr = r.json()["id"]
    mine = s.toolset.find_open_work_order(AGENCY, "veh-1")
    assert mine.status == "ok" and mine.value is not None and mine.value.id == pr
    assert mine.value.asset_vehicle_id == "veh-1"
    s.clock.advance(hours=2)
    got = s.toolset.find_open_work_order(AGENCY, "veh-2")
    assert got.status == "ok" and got.value is None, got


def test_j1_pending_repair_without_a_request_time_is_refused(world):
    s = _swap_1106(world, pending=True)
    r = world.mocks.clients["maintenance"].post("/pending-repairs", headers=H,
                                                json={"asset": "1106", "description": "noise", "requested_by": "human"})
    pr = r.json()["id"]
    store = world.mocks.apps["maintenance"].state.ctx.store
    store.put(AGENCY, "pending_repair", pr, {**store.get(AGENCY, "pending_repair", pr), "created_at": None})
    refused = s.toolset.find_open_work_order(AGENCY, "veh-1")
    assert refused.status == "unavailable", refused
