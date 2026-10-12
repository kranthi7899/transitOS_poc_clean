"""agency-088 (Phase 10 Step 2.2): a generated fleet whose identifiers disagree across the SIMULATED products.

The vehicle crosswalk is worked out by discovery from the products' own rosters (never from generator truth); the
`truth_*` keys of the generated fleet are read HERE only, to check the answer.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from transitos.contracts.envelope import TOPIC_FAILED, TOPIC_RESOLVED
from transitos.integration.system import Mocks, System
from transitos.mocks.clock import SimClock, parse_ts
from transitos.mocks.crosswalk_seed import crosswalk_entries
from transitos.mocks.fleet_088 import CUTAWAY_IDX, SWAP_IDX, TYPO_VIN_IDX
from transitos.mocks.seed import build_fleet
from transitos.plumbing.discovery import anchor_canonical

AG = "agency-088"
T0 = datetime(2026, 10, 10, 6, 0, tzinfo=UTC)
H = {"X-Agency": AG}


@pytest.fixture(scope="module")
def fleet():
    return build_fleet(AG, T0)


@pytest.fixture
def system(tmp_path):
    m = Mocks.start(SimClock(T0))
    m.seed(AG)
    s = System(AG, m, tmp_path / "t088")
    yield s
    s.close()


def truth_canonical(v: dict) -> str:
    return anchor_canonical(f"BUS-{int(v['truth_fleet_number'].split('-')[1]):03d}")


def by_role(fleet, role):
    return [v for v in fleet.vehicles if v["role"] == role]


def test_generated_fleet_is_deterministic():
    a, b = build_fleet(AG, T0), build_fleet(AG, T0)
    assert json.dumps([a.vehicles, a.blocks, a.operators, a.pm], default=str) == \
        json.dumps([b.vehicles, b.blocks, b.operators, b.pm], default=str)


def test_generated_fleet_has_the_deliberate_mess(fleet):
    rev = [v for v in fleet.vehicles if v["sched"]]
    assert len(rev) == 12
    # inconsistent formatting of the same fleet number across products
    assert any(v["tele_name"] != v["sched_name"] for v in rev) and any(v["maint_name"] != v["sched_name"] for v in rev)
    assert any(v["maint"] != v["sched"].split("-")[1] for v in rev)            # zero padding differs
    assert any(" " in (v["maint_vin"] or "") or (v["maint_vin"] or "").islower() for v in rev)
    assert fleet.vehicles[TYPO_VIN_IDX - 1]["maint_vin"] != fleet.vehicles[TYPO_VIN_IDX - 1]["truth_vin"]
    [retired] = by_role(fleet, "retired")
    assert retired["sched"] is None and retired["tele"] is None and retired["maint_status"] == "retired"
    [truck] = by_role(fleet, "non_revenue")
    assert truck["sched"] is None and truck["tele"] and truck["maint"]
    [orphan] = by_role(fleet, "orphan")
    assert orphan["tele"] and orphan["maint"] is None and orphan["sched"] is None
    swap = fleet.vehicles[SWAP_IDX - 1]
    assert swap["tele_prev"]["removed_at"] == swap["tele_installed_at"]
    cut = fleet.vehicles[CUTAWAY_IDX - 1]
    assert cut["type"] == "cutaway" and "chassis" in cut["make"] and cut["maint_make"] != cut["make"]


def test_crosswalk_seed_does_not_derive_vehicle_rows_from_generator_truth():
    rows = crosswalk_entries(AG, T0)
    assert rows and all(r.entity == "operator" for r in rows)


def test_discovery_works_out_every_revenue_vehicle_from_product_reads(system, fleet):
    rows = {(e.product, e.native_id, e.valid_from, e.valid_to): e.canonical_id for e in system.xw.rows(AG)
            if e.entity != "operator"}
    for v in [v for v in fleet.vehicles if v["sched"]]:
        want = truth_canonical(v)
        assert rows[("scheduling", v["sched"], None, None)] == want
        assert rows[("maintenance", v["maint"], None, None)] == want, v["idx"]
        installed = parse_ts(v["tele_installed_at"]) if v.get("tele_installed_at") else None
        assert rows[("telematics", v["tele"], installed, None)] == want, v["idx"]
        if prev := v.get("tele_prev"):
            assert rows[("telematics", prev["id"], None, parse_ts(prev["removed_at"]))] == want
    report = {(r["product"], r["native_id"]): r for r in system.discovery}
    typo = fleet.vehicles[TYPO_VIN_IDX - 1]
    assert report[("maintenance", typo["maint"])]["outcome"] == "MATCH"
    assert any("check digit" in n for n in report[("maintenance", typo["maint"])]["notes"])


def test_retired_non_revenue_and_orphan_records_get_no_canonical_vehicle(system, fleet):
    report = {(r["product"], r["native_id"]): r for r in system.discovery}
    [retired], [truck], [orphan] = by_role(fleet, "retired"), by_role(fleet, "non_revenue"), by_role(fleet, "orphan")
    assert report[("maintenance", retired["maint"])]["outcome"] != "MATCH"   # Bus 39 is one digit off Bus 49: a 0.6 lone candidate, a human decides
    assert report[("maintenance", truck["maint"])]["outcome"] == "NO_MATCH"
    assert report[("telematics", truck["tele"])]["outcome"] == "NO_MATCH"
    assert report[("telematics", orphan["tele"])]["outcome"] == "NO_MATCH"
    mapped = {e.native_id for e in system.xw.rows(AG)}
    assert not {retired["maint"], truck["maint"], truck["tele"], orphan["tele"]} & mapped


def test_retired_asset_is_never_a_spare_and_never_gets_a_work_order(system, fleet):
    """Review Focus 5: not in the revenue roster (so never a spare), no PM schedule (so no PMOverdue), and the
    maintenance product refuses a work order against it."""
    [retired] = by_role(fleet, "retired")
    c = system.mocks.clients
    ids = {v["id"] for v in c["scheduling"].get("/vehicles", headers=H).json()}
    assert len(ids) == 12 and not any(retired["truth_fleet_number"].split("-")[1] in i for i in ids)
    assert retired["maint"] not in {p["asset"] for p in c["maintenance"].get("/pm-schedules", headers=H).json()}
    r = c["maintenance"].post("/workorders", headers=H, json={"asset": retired["maint"], "description": "x"})
    assert r.status_code == 409 and "retired" in r.text


def fire(system, unit: str, spn: int = 110) -> None:
    system.mocks.clients["telematics"].post("/sim/fault", headers=H, json={
        "vehicle": unit, "spn": spn, "fmi": 0, "lamps": {"stop": True}})
    system.run_until_idle()


def test_a_fault_from_a_dirty_named_unit_resolves_to_its_bus_through_the_webhook(system, fleet):
    v = fleet.vehicles[2]
    fire(system, v["tele"])
    resolved = [e for _, _, e in system.bus.messages(TOPIC_RESOLVED) if e.type == "VehicleFaultDetected"]
    assert [e.canonicalid for e in resolved] == [truth_canonical(v)]
    assert resolved[0].data["native_name"] == v["tele_name"]       # the adapter carried the evidence


def test_orphan_unit_fault_is_parked_never_guessed(system, fleet):
    [orphan] = by_role(fleet, "orphan")
    fire(system, orphan["tele"])
    assert not [e for _, _, e in system.bus.messages(TOPIC_RESOLVED) if e.type == "VehicleFaultDetected"]
    failed = [e for _, _, e in system.bus.messages(TOPIC_FAILED)]
    assert len(failed) == 1 and failed[0].failurereason.startswith("unresolved vehicle telematics:")
    assert not system.runs()


def test_old_gateway_after_the_swap_is_parked_and_before_it_resolves(system, fleet):
    """Review Focus 4 through the webhook and the real crosswalk rows discovery wrote."""
    v = fleet.vehicles[SWAP_IDX - 1]
    removed = parse_ts(v["tele_prev"]["removed_at"])
    xw = system.xw
    assert xw.lookup(AG, "telematics", v["tele_prev"]["id"], at=removed - timedelta(microseconds=1)) == \
        truth_canonical(v)
    assert xw.lookup(AG, "telematics", v["tele_prev"]["id"], at=removed) is None
    assert xw.lookup(AG, "telematics", v["tele"], at=removed) == truth_canonical(v)
    fire(system, v["tele_prev"]["id"])              # the sim clock is after the swap
    assert not [e for _, _, e in system.bus.messages(TOPIC_RESOLVED) if e.type == "VehicleFaultDetected"]
    [u] = system.xw.unresolved(AG)
    assert u["native_id"] == v["tele_prev"]["id"]
    assert u["resolution"]["outcome"] in ("AMBIGUOUS", "NO_MATCH")
    assert any("no crosswalk row valid" in n for n in u["resolution"]["notes"])
