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


def test_g3_seeded_asset_ids_are_unchanged_without_reuse(world):
    """Converse: the seeded rows (open, no validity) still read as before."""
    world.apply(BY_ID["retired_spare_never_proposed"])
    got = world.system.toolset.asset_statuses(AGENCY, ["veh-1", "veh-2", "veh-3", "veh-404"])
    assert got.value == {"veh-1": "active", "veh-2": "retired", "veh-3": "active", "veh-404": "missing"}
