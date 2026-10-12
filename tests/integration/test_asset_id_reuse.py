"""Bug sweep A, G3 (A147-A148) through the REAL mocks and System: a maintenance asset id that ended for one vehicle
and was given to another. Written from the ruling before the code:
  - all_ids honours validity (at the System clock's now) and entity, so `asset_statuses` reads a vehicle's status
    only through a row valid NOW;
  - the vehicle whose row ended (veh-2, retired, its number reused) has NO valid maintenance row: its status is
    unknown ("missing"), which the clearance rule `asset_active` turns into unverifiable (never 'active');
  - the vehicle that now holds the number (veh-3) is read through its own current row (asset 1107, active), never
    through its own ended row (asset 1108, retired)."""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from transitos.clearance.facts import Facts
from transitos.clearance.rules import asset_active_item
from transitos.contracts.crosswalk import CrosswalkEntry
from transitos.contracts.objects import Vehicle
from transitos.integration.world import IntegrationWorld
from transitos.verification.scenarios import AGENCY, BY_ID


@pytest.fixture
def world():
    w = IntegrationWorld()
    yield w
    if w.system is not None:
        w.system.close()


def _reuse_asset_1107(world) -> None:
    """Seeded: veh-2 -> asset 1107 (active), veh-3 -> asset 1108 (retired in maintenance). An hour ago veh-2 left
    service and its asset number 1107 was given to veh-3; veh-3's old number 1108 ended at the same time."""
    sc = BY_ID["retired_spare_never_proposed"]
    world.apply(replace(sc, tweaks=replace(sc.tweaks, spares=2, retired_spare=3)))
    xw, t = world.system.xw, world.system.clock.now() - timedelta(hours=1)

    def row(native: str, canon: str, **kw) -> CrosswalkEntry:
        return CrosswalkEntry(agency=AGENCY, product="maintenance", native_id=native, entity="asset",
                              canonical_id=canon, **kw)
    xw.resolve(row("1107", "veh-2", valid_to=t))       # same key as the seeded row: it now ends at t
    xw.resolve(row("1108", "veh-3", valid_to=t))
    xw.resolve(row("1107", "veh-3", valid_from=t))


def test_g3_reused_asset_id_ended_vehicle_is_unknown_and_the_new_owner_reads_its_own_row(world):
    _reuse_asset_1107(world)
    got = world.system.toolset.asset_statuses(AGENCY, ["veh-2", "veh-3"])
    assert got.status == "ok"
    assert got.value == {"veh-2": "missing", "veh-3": "active"}, got.value


def test_g3_the_ended_vehicle_is_unverifiable_for_clearance_never_active(world):
    _reuse_asset_1107(world)
    statuses = world.system.toolset.asset_statuses(AGENCY, ["veh-2", "veh-3"]).value
    facts = Facts(blocks={}, vehicles={}, operators={}, ready_count=None, commitments=None, asset_status=statuses,
                  ready_unknown=0)
    v2 = Vehicle(id="veh-2", agency=AGENCY, depot="North", type="standard", seats=40, wheelchair_positions=2,
                 lift_ok=True, status="ready")
    it = asset_active_item(v2, facts)
    assert (it.status, it.basis, it.requires_ack) == ("unverifiable", "unknown", True), it
    assert asset_active_item(v2.model_copy(update={"id": "veh-3"}), facts).status == "pass"


def _row(native: str, canon: str, **kw) -> CrosswalkEntry:
    return CrosswalkEntry(agency=AGENCY, product="maintenance", native_id=native, entity="asset", canonical_id=canon,
                          **kw)


def test_h1_reused_asset_id_reverse_map_and_work_order_label_follow_the_clock(world):
    """Bug sweep A2, H1 (A159), written from the ruling: asset 1106 is veh-1's until S (an hour from now) and veh-2's
    from S. The reverse map (native -> vehicle) uses the System clock: veh-1 before S, veh-2 at and after S. A work
    order veh-1 opened on 1106 is read back for veh-1 labelled veh-1, and is NEVER labelled veh-2 for veh-1."""
    sc = BY_ID["retired_spare_never_proposed"]
    world.apply(replace(sc, tweaks=replace(sc.tweaks, spares=2, retired_spare=None)))
    s, xw = world.system, world.system.xw
    swap = s.clock.now() + timedelta(hours=1)
    xw.resolve(_row("1106", "veh-1", valid_to=swap))
    xw.resolve(_row("1106", "veh-2", valid_from=swap))
    xw.resolve(_row("1107", "veh-2", valid_to=swap))
    r = world.mocks.clients["maintenance"].post("/workorders", headers={"X-Agency": AGENCY},
                                                json={"asset": "1106", "description": "noise", "opened_by": "human"})
    assert r.status_code < 300, r.text
    assert xw.lookup(AGENCY, "maintenance", "1106") == "veh-1"                    # before the swap
    wo = s.toolset.find_open_work_order(AGENCY, "veh-1")
    assert wo.status == "ok" and wo.value is not None and wo.value.asset_vehicle_id == "veh-1", wo
    s.clock.advance(hours=1)                                                       # at the swap
    assert xw.lookup(AGENCY, "maintenance", "1106") == "veh-2"
    s.clock.advance(hours=1)                                                       # after it
    assert xw.lookup(AGENCY, "maintenance", "1106") == "veh-2"
    after = s.toolset.find_open_work_order(AGENCY, "veh-1")                       # veh-1 has no asset now: unknown
    assert after.value is None or after.value.asset_vehicle_id != "veh-2", after
    assert after.status != "ok", after


def test_h2_two_valid_assets_for_one_vehicle_are_unknown_not_the_newest(world):
    """H2 (A160): veh-2 keeps its (retired) 1107 row and is also given 1108 (veh-3's old, active asset) without
    closing 1107. The newest row must not hide the retired one: veh-2's status is unknown (missing) -> unverifiable."""
    sc = BY_ID["retired_spare_never_proposed"]
    world.apply(replace(sc, tweaks=replace(sc.tweaks, spares=2, retired_spare=2)))
    xw, t = world.system.xw, world.system.clock.now() - timedelta(hours=1)
    xw.resolve(_row("1108", "veh-3", valid_to=t))
    xw.resolve(_row("1108", "veh-2", valid_from=t))
    got = world.system.toolset.asset_statuses(AGENCY, ["veh-2"]).value
    assert got == {"veh-2": "missing"}, got


def test_h2_one_asset_valid_for_two_vehicles_is_unknown_for_both_and_never_targeted(world):
    """H2 (A160): asset 1107 is valid for veh-2 AND veh-3 (veh-3's own 1108 closed). Neither reads 1107's status, the
    reverse map names no single vehicle, and a work order on 1107 is not labelled with either (targeting refuses)."""
    sc = BY_ID["retired_spare_never_proposed"]
    world.apply(replace(sc, tweaks=replace(sc.tweaks, spares=2, retired_spare=None)))
    s, xw, t = world.system, world.system.xw, world.system.clock.now() - timedelta(hours=1)
    xw.resolve(_row("1108", "veh-3", valid_to=t))
    xw.resolve(_row("1107", "veh-3", valid_from=t))
    assert s.toolset.asset_statuses(AGENCY, ["veh-2", "veh-3"]).value == {"veh-2": "missing", "veh-3": "missing"}
    assert xw.lookup(AGENCY, "maintenance", "1107") is None
    world.mocks.clients["maintenance"].post("/workorders", headers={"X-Agency": AGENCY},
                                            json={"asset": "1107", "description": "noise", "opened_by": "human"})
    raw = world.mocks.clients["maintenance"].get("/workorders", headers={"X-Agency": AGENCY},
                                                 params={"asset": "1107"}).json()
    from transitos.tools.common import ProductUnavailable
    from transitos.tools.read_adapters import map_work_order
    with pytest.raises(ProductUnavailable):
        map_work_order(_deps(s), AGENCY, raw[0])     # the adapter's own mapper, on the System's crosswalk
    for vid in ("veh-2", "veh-3"):
        assert s.toolset.find_open_work_order(AGENCY, vid).status != "ok"


def _deps(s):
    from types import SimpleNamespace
    return SimpleNamespace(crosswalk=s.xw, settings_for=lambda a: {})


def test_g3_seeded_asset_ids_are_unchanged_without_reuse(world):
    """Converse: the seeded rows (open, no validity) still read as before."""
    world.apply(BY_ID["retired_spare_never_proposed"])
    got = world.system.toolset.asset_statuses(AGENCY, ["veh-1", "veh-2", "veh-3", "veh-404"])
    assert got.value == {"veh-1": "active", "veh-2": "retired", "veh-3": "active", "veh-404": "missing"}
