"""Phase 10 Step 4 (Track C) against the REAL mock products: pending-repair mode of create_work_order, the shop
status map (A23 extended), and zone-less local time read with the agency timezone (never guessed as UTC)."""
from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import BaseModel

from transitos.contracts.objects import Needs
from transitos.mocks.crosswalk_seed import crosswalk_entries
from transitos.tools.common import StaticCrosswalk
from transitos.tools.idem_store import IdemStore
from transitos.tools.policy import KillSwitchStore
from transitos.tools.read_adapters import map_work_order
from transitos.tools.registry import READ_TOOLS, WRITE_TOOLS, Registry

from .conftest import AG, H

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

NY = "America/New_York"
PENDING = {"maintenance": {"mode": "pending_repair"}}
LOCAL = {"local_time": True, "timezone": NY}
W0, W1 = datetime(2026, 1, 5, 7, 0, tzinfo=UTC), datetime(2026, 1, 5, 9, 0, tzinfo=UTC)


def crosswalk() -> StaticCrosswalk:
    xw = StaticCrosswalk()
    for e in crosswalk_entries(AG):
        xw.add(AG, e.canonical_id, **{e.product: e.native_id})
    return xw


def tools(mocks, settings: dict | None = None):
    patch = {AG: dict(settings or {})}
    reg = Registry(clients={n: mocks.c(n) for n in ("maintenance", "scheduling", "workforce", "alerts")},
                   crosswalk=crosswalk(), idem=IdemStore(), killswitch=KillSwitchStore(), sleep=lambda s: None,
                   settings_patch=patch)
    return reg.toolset(AG), patch


def mlocal(mocks, offsets: bool = True) -> None:
    for p in ("maintenance", "workforce", "telematics"):
        mocks.c(p).post("/sim/time-format", json={"local_tz": NY, "utc_offset": offsets, "epoch_ms": True}, headers=H)


def prs(mocks) -> list[dict]:
    return mocks.c("maintenance").get("/pending-repairs", headers=H).json()


def wos(mocks) -> list[dict]:
    return mocks.c("maintenance").get("/workorders", headers=H).json()


# ------------------------------------------------------------------ 4.1 status map (A23 extended, A107)
@pytest.mark.parametrize(("native", "canonical"), [
    ("OPEN", "open"), ("IN_PROGRESS", "in_progress"), ("WAITING_PARTS", "in_progress"), ("PENDING", "open"),
    ("COMPLETE", "in_progress"), ("CLOSED", "closed"),
    ("open", "open"), ("in_progress", "in_progress"), ("closed", "closed")])
def test_shop_status_map(native, canonical):
    from types import SimpleNamespace
    xw = crosswalk()
    raw = {"id": "wo-1", "wo_no": "WO-5001", "asset": "1106", "status": native, "opened_by": "system",
           "opened_at": "2026-01-05T06:00:00Z", "closed_at": None}
    assert map_work_order(SimpleNamespace(crosswalk=xw), AG, raw).status == canonical


@pytest.mark.parametrize("status", ["WAITING_PARTS", "PENDING", "COMPLETE", "IN_PROGRESS"])
def test_a_shop_work_order_that_is_not_closed_is_still_the_open_work_order(mocks, status):
    ts, _ = tools(mocks, PENDING)
    m = mocks.c("maintenance")
    pr = m.post("/pending-repairs", json={"asset": "1106", "requested_by": "system"}, headers=H).json()
    wo = m.post(f"/sim/pending-repairs/{pr['id']}/convert", headers=H).json()["work_order"]
    m.post(f"/sim/workorders/{wo['id']}/status", json={"status": status}, headers=H)
    r = ts.find_open_work_order(AG, "veh-1")
    assert r.status == "ok" and r.value is not None and r.value.id == wo["id"]


# ------------------------------------------------------------------ 4.1 pending-repair mode of create_work_order
def test_default_mode_is_unchanged_a_work_order_is_posted(mocks):
    ts, _ = tools(mocks)
    r = ts.create_work_order(AG, "veh-1", "pull", "run:create_work_order:1")
    assert r.status == "ok" and r.value.id.startswith("wo-")
    assert prs(mocks) == [] and len(wos(mocks)) == 1


def test_pending_mode_creates_a_pending_repair_not_a_work_order(mocks):
    ts, _ = tools(mocks, PENDING)
    r = ts.create_work_order(AG, "veh-1", "coolant", "run:create_work_order:1")
    assert r.status == "ok", r.note
    assert r.value.id == prs(mocks)[0]["id"] and r.value.status == "open" and r.value.source == "system"
    assert "pending_repair" in (r.note or "")
    assert wos(mocks) == [] and prs(mocks)[0]["requested_by"] == "system"


def test_pending_mode_replay_by_key_reads_back_the_converted_work_order_without_a_duplicate(mocks):
    ts, _ = tools(mocks, PENDING)
    key = "run:create_work_order:1"
    first = ts.create_work_order(AG, "veh-1", "coolant", key).value
    again = ts.create_work_order(AG, "veh-1", "coolant", key).value      # same key before conversion
    assert again.id == first.id and len(prs(mocks)) == 1
    wo = mocks.c("maintenance").post(f"/sim/pending-repairs/{first.id}/convert", headers=H).json()["work_order"]
    later = ts.create_work_order(AG, "veh-1", "coolant", key)            # same key after conversion
    assert later.status == "ok" and later.value.id == first.id           # one effect per key (A106)
    assert later.value.native_no == wo["wo_no"] and wo["wo_no"] in (later.note or "")
    assert len(prs(mocks)) == 1 and len(wos(mocks)) == 1
    got = ts.get_work_order(AG, first.id)
    assert got.status == "ok" and got.value.native_no == wo["wo_no"]


def test_pending_mode_reuses_a_work_order_a_person_opened_first(mocks):
    ts, _ = tools(mocks, PENDING)
    human = mocks.c("maintenance").post("/workorders", json={"asset": "1106", "opened_by": "human"}, headers=H).json()
    r = ts.create_work_order(AG, "veh-1", "coolant", "k1")
    assert r.status == "ok" and r.value.id == human["id"] and r.note == "already_open"
    assert prs(mocks) == []                                               # no pending repair, no duplicate


def test_pending_mode_reuses_an_open_pending_repair(mocks):
    ts, _ = tools(mocks, PENDING)
    a = ts.create_work_order(AG, "veh-1", "coolant", "k1").value
    b = ts.create_work_order(AG, "veh-1", "coolant", "k2").value          # another key (e.g. a new plan version)
    assert a.id == b.id and len(prs(mocks)) == 1


def test_pending_mode_compensation(mocks):
    ts, _ = tools(mocks, PENDING)
    m = mocks.c("maintenance")
    # 1) still pending: the request is withdrawn
    a = ts.create_work_order(AG, "veh-1", "coolant", "k1").value
    r = ts.cancel_work_order(AG, a.id, "c1")
    assert r.status == "ok" and r.value.status == "closed"
    assert m.get(f"/pending-repairs/{a.id}", headers=H).json()["status"] == "CANCELLED"
    # 2) converted into its own work order: that work order is cancelled
    b = ts.create_work_order(AG, "veh-2", "x", "k2").value
    wo = m.post(f"/sim/pending-repairs/{b.id}/convert", headers=H).json()["work_order"]
    r = ts.cancel_work_order(AG, b.id, "c2")
    assert r.status == "ok" and r.value.status == "closed"
    w = m.get(f"/workorders/{wo['id']}", headers=H).json()
    assert w["status"] == "CLOSED" and w["cancelled"] is True
    # 3) attached to a person's work order: the request is withdrawn, the person's work order untouched
    c = ts.create_work_order(AG, "veh-3", "x", "k3").value
    human = m.post("/workorders", json={"asset": "1108", "opened_by": "human"}, headers=H).json()
    m.post(f"/sim/pending-repairs/{c.id}/convert", headers=H)
    r = ts.cancel_work_order(AG, c.id, "c3")
    assert r.status == "ok"
    assert m.get(f"/workorders/{human['id']}", headers=H).json()["status"] == "open"
    assert m.get(f"/pending-repairs/{c.id}", headers=H).json()["status"] == "CANCELLED"


# ------------------------------------------------------------------ 4.2 local time (A108, A109)
def test_local_time_read_normalises_to_utc(mocks):
    mlocal(mocks)
    ts, _ = tools(mocks, LOCAL)
    mocks.c("maintenance").post("/workorders", json={"asset": "1106", "opened_by": "human"}, headers=H)
    r = ts.find_open_work_order(AG, "veh-1")
    assert r.status == "ok", r.note
    assert r.value.opened_at == mocks.clock.now() and r.value.opened_at.tzinfo is not None


def _fallback(mocks):
    mocks.clock.advance(hours=(datetime(2026, 11, 1, 6, 30, tzinfo=UTC) - mocks.clock.now()).total_seconds() / 3600)
    assert mocks.clock.now() == datetime(2026, 11, 1, 6, 30, tzinfo=UTC)     # 01:30 EST, the SECOND 01:30


def test_fall_back_second_occurrence_is_read_correctly_when_the_product_sends_its_offset(mocks):
    _fallback(mocks)
    mlocal(mocks, offsets=True)
    ts, _ = tools(mocks, LOCAL)
    mocks.c("maintenance").post("/workorders", json={"asset": "1106", "opened_by": "human"}, headers=H)
    assert wos(mocks)[0]["opened_at"] == "2026-11-01T01:30:00"
    assert ts.find_open_work_order(AG, "veh-1").value.opened_at == datetime(2026, 11, 1, 6, 30, tzinfo=UTC)


def test_fall_back_without_an_offset_follows_the_documented_first_occurrence_rule(mocks):
    _fallback(mocks)
    mlocal(mocks, offsets=False)
    ts, _ = tools(mocks, LOCAL)
    mocks.c("maintenance").post("/workorders", json={"asset": "1106", "opened_by": "human"}, headers=H)
    # the product says only "01:30"; the documented rule reads the first 01:30 (EDT): an hour early, by design
    assert ts.find_open_work_order(AG, "veh-1").value.opened_at == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)


@pytest.mark.parametrize("tz", [None, "", "Mars/Olympus_Mons"])
def test_unknown_agency_timezone_makes_the_read_unavailable_never_utc(mocks, tz):
    mlocal(mocks)
    ts, _ = tools(mocks, {"local_time": True, "timezone": tz})
    mocks.c("maintenance").post("/workorders", json={"asset": "1106", "opened_by": "human"}, headers=H)
    r = ts.find_open_work_order(AG, "veh-1")
    assert r.status == "unavailable" and "timezone" in (r.note or "")
    blk = ts.list_affected_blocks(AG, "veh-1", W0, W1).value[0]
    r = ts.list_eligible_operators(AG, blk)
    assert r.status == "unavailable" and "timezone" in (r.note or "")


def test_mixed_formats_a_zoned_string_under_the_local_setting_is_refused(mocks):
    ts, _ = tools(mocks, LOCAL)          # the agency says local time, the product sends UTC with a zone
    mocks.c("maintenance").post("/workorders", json={"asset": "1106", "opened_by": "human"}, headers=H)
    r = ts.find_open_work_order(AG, "veh-1")
    assert r.status == "unavailable" and "zone" in (r.note or "")


def test_a_zone_less_string_without_the_local_setting_is_never_read_as_utc(mocks):
    mlocal(mocks)
    ts, _ = tools(mocks)                 # the product sends local time, the agency config does not say so
    mocks.c("maintenance").post("/workorders", json={"asset": "1106", "opened_by": "human"}, headers=H)
    r = ts.find_open_work_order(AG, "veh-1")
    assert r.status == "unavailable" and "zone" in (r.note or "")
    blk = ts.list_affected_blocks(AG, "veh-1", W0, W1).value[0]
    assert ts.list_eligible_operators(AG, blk).status == "unavailable"


def test_workforce_csv_local_time_gives_the_same_operators_as_utc(mocks):
    ts0, _ = tools(mocks)
    blk = ts0.list_affected_blocks(AG, "veh-1", W0, W1).value[0]
    before = ts0.list_eligible_operators(AG, blk).value
    mlocal(mocks)
    ts1, _ = tools(mocks, LOCAL)
    after = ts1.list_eligible_operators(AG, blk).value
    assert [o.model_dump() for o in after] == [o.model_dump() for o in before]


# ------------------------------------------------------------------ the no-naive-output walk (spec 4.2)
TS_STR = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(?P<zone>Z|[+-]\d{2}:\d{2})?$")


def naive_values(x: Any, path: str = "$") -> list[str]:
    """Every datetime without a zone, and every timestamp-shaped string without one, anywhere in `x`."""
    if isinstance(x, BaseModel):
        return naive_values(x.model_dump(), path)
    if isinstance(x, datetime):
        return [] if x.tzinfo is not None and x.utcoffset() is not None else [f"{path}={x!r}"]
    if isinstance(x, str):
        m = TS_STR.match(x)
        return [f"{path}={x!r}"] if m and not m.group("zone") else []
    if isinstance(x, dict):
        return [v for k, val in x.items() for v in naive_values(val, f"{path}.{k}")]
    if isinstance(x, (list, tuple, set)):
        return [v for i, val in enumerate(x) for v in naive_values(val, f"{path}[{i}]")]
    if hasattr(x, "to_dict"):
        return naive_values(x.to_dict(), path)
    return []


def test_the_walker_finds_naive_values():
    assert naive_values({"a": [datetime(2026, 1, 1)]}) and naive_values({"b": "2026-01-01T01:30:00"})
    assert not naive_values({"a": datetime(2026, 1, 1, tzinfo=UTC), "b": "2026-01-01T01:30:00Z", "c": "OPEN"})


def test_no_adapter_output_contains_a_naive_timestamp_with_the_knob_on(mocks):
    """Walk EVERY read tool, optional read and write tool with the products sending zone-less local time."""
    mlocal(mocks)
    ts, _ = tools(mocks, {**LOCAL, **PENDING})
    m = mocks.c("maintenance")
    m.post("/workorders", json={"asset": "1107", "opened_by": "human"}, headers=H)   # a human WO on veh-2
    blk = ts.list_affected_blocks(AG, "veh-1", W0, W1).value[0]
    ops = ts.list_eligible_operators(AG, blk).value
    calls: dict[str, Any] = {
        "get_vehicle": lambda: ts.get_vehicle(AG, "veh-1"),
        "get_vehicle_history": lambda: ts.get_vehicle_history(AG, "veh-2"),
        "list_affected_blocks": lambda: ts.list_affected_blocks(AG, "veh-1", W0, W1),
        "list_ready_vehicles": lambda: ts.list_ready_vehicles(AG, W0, W1, Needs(seats=20, type="standard")),
        "list_eligible_operators": lambda: ts.list_eligible_operators(AG, blk),
        "find_open_work_order": lambda: ts.find_open_work_order(AG, "veh-2"),
        "get_work_order": lambda: ts.get_work_order(AG, wos(mocks)[0]["id"]),
        "create_work_order": lambda: ts.create_work_order(AG, "veh-1", "coolant", "w:create:1"),
        "cancel_work_order": lambda: ts.cancel_work_order(AG, prs(mocks)[0]["id"], "w:cancel:1"),
        "set_vehicle_unavailable": lambda: ts.set_vehicle_unavailable(AG, "veh-1", W0, None, "w:unavail:1"),
        "reassign_block": lambda: ts.reassign_block(AG, blk.id, "veh-10", "w:reassign:1"),
        "assign_operator": lambda: ts.assign_operator(AG, blk.id, ops[0].id, "w:assign:1"),
        "publish_rider_alert": lambda: ts.publish_rider_alert(AG, blk.id, "Bus swapped", "w:alert:1"),
        "set_vehicle_available": lambda: ts.set_vehicle_available(AG, "veh-1", "w:avail:1"),
        # optional reads (not in ReadTools)
        "list_blocks": lambda: ts.list_blocks(AG, W0, W1),
        "asset_statuses": lambda: ts.asset_statuses(AG, ["veh-1", "veh-2"]),
        "capability_profiles": lambda: ts.capability_profiles(AG),
    }
    assert set(READ_TOOLS) | set(WRITE_TOOLS) <= set(calls)     # nothing in the tool surface is left unwalked
    found, results = [], {}
    for name, fn in calls.items():
        res = fn()
        results[name] = res.status
        found += naive_values(res.value, name)
    assert results["create_work_order"] == "ok" and results["find_open_work_order"] == "ok", results
    assert results["list_eligible_operators"] == "ok" and results["get_vehicle_history"] == "ok", results
    assert not found, found
    # get_vehicle_history carries product times as strings: they come back zoned, in UTC
    hist = ts.get_vehicle_history(AG, "veh-2").value
    assert any(h["kind"] == "work_order" and h["at"].endswith("Z") for h in hist)
