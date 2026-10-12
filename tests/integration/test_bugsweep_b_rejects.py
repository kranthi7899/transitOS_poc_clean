"""Bug sweep B, F7-R4 (A155-A157): per-record isolation in the watch adapters, through the REAL pollers and the REAL
mock products. One unreadable record is NOT emitted, NOT silently dropped (a `poll_rejects` row: product, native id,
reason, payload digest, first/last seen sim time), and does NOT block the other records: the rest of the page is
emitted and the cursor moves. Seen again, it is re-evaluated: a corrected record flows and its row is resolved.
An unknown agency timezone is configuration, not a record: the poll still HOLDS (A108, unchanged).
Expected rows are written from the ruling text."""
from __future__ import annotations

import re

import pytest

from transitos.contracts.envelope import TOPIC_RAW
from transitos.plumbing import Db, Relay, SqliteBus
from transitos.plumbing.pg import split_script, translate
from transitos.plumbing.watch import MaintenancePoller, TelematicsBackfillPoller
from transitos.plumbing.watch import base as wbase
from transitos.plumbing.watch.base import list_rejects, load_state

from .conftest import AG, H

DIGEST = re.compile(r"^[0-9a-f]{12}$")


@pytest.fixture
def db(tmp_path):
    return Db(str(tmp_path / "b.db"))


def raw(db):
    bus = SqliteBus(db)
    Relay(db, bus).drain()
    return [e for _, _, e in bus.messages(TOPIC_RAW)]


# ------------------------------------------------------------------ telematics backfill page
def _three_faults(mocks) -> list[dict]:
    t = mocks.c("telematics")
    return [t.post("/sim/fault", json={"vehicle": v, "spn": 110, "fmi": 0}, headers=H).json()
            for v in ("T-77", "T-78", "T-79")]


def _break_item(mocks, event_id: str, time) -> None:
    st = mocks.apps["telematics"].state.ctx.store
    for e in st.all(AG, "events"):
        if e["payload"]["eventId"] == event_id:
            e["payload"]["time"] = time
            st.put(AG, "events", f"{e['seq']:08d}", e)


@pytest.mark.parametrize(("bad", "why"), [("2026-10-10T09:40:00", "zone"),        # zone-less string
                                          (1_793_500_200, "1793500200"),          # epoch SECONDS (A146)
                                          (0, "outside")])                        # out-of-range epoch
def test_a_page_with_one_bad_item_emits_the_two_good_ones_and_one_reject(db, mocks, bad, why):
    good1, broken, good2 = _three_faults(mocks)
    _break_item(mocks, broken["eventId"], bad)
    p = TelematicsBackfillPoller(db, mocks.c("telematics"), AG)
    p.clock = mocks.clock.now
    assert p.poll() == 2
    assert p.last_error is None                                       # last_error keeps its meaning: the poll ran
    evs = raw(db)
    assert sorted(e.data["native_vehicle_id"] for e in evs) == ["T-77", "T-79"]
    (row,) = list_rejects(db)
    assert (row["product"], row["native_id"], row["agency"], row["resolved"]) == ("telematics", broken["eventId"], AG, 0)
    assert why in row["reason"] and DIGEST.match(row["digest"])
    assert row["first_seen"] == row["last_seen"] == mocks.clock.now().isoformat()
    cursor, _ = load_state(db, p.name)
    # fix round N1 b (A166), deliberately changed: the cursor waits at the page the open reject came on (was: moved
    # past it), so the item is read again until it is corrected
    assert cursor is None
    st = p.status()
    assert st["last_error"] is None and st["rejects"]["open"] == 1 and why in st["rejects"]["reasons"][0]
    assert {good1["eventId"], good2["eventId"]} == {r["vendor_event_id"] for r in
                                                    db.execute("SELECT vendor_event_id FROM telematics_seen")}


def test_a_rejected_item_is_one_row_however_often_it_is_seen(db, mocks):
    (_, broken, _) = _three_faults(mocks)
    _break_item(mocks, broken["eventId"], "2026-10-10T09:40:00")
    p = TelematicsBackfillPoller(db, mocks.c("telematics"), AG)
    p.clock = mocks.clock.now
    first = mocks.clock.now().isoformat()
    p.poll()
    db.execute("UPDATE adapter_state SET cursor=NULL WHERE name=?", (p.name,))   # the vendor re-sends the page
    mocks.clock.advance(minutes=5)
    assert p.poll() == 0                                              # the good two are not emitted twice
    (row,) = list_rejects(db)
    assert row["first_seen"] == first and row["last_seen"] == mocks.clock.now().isoformat()


def test_without_a_clock_the_seen_times_are_unknown_never_the_wall_clock(db, mocks):
    (_, broken, _) = _three_faults(mocks)
    _break_item(mocks, broken["eventId"], "2026-10-10T09:40:00")
    p = TelematicsBackfillPoller(db, mocks.c("telematics"), AG)
    assert p.poll() == 2
    (row,) = list_rejects(db)
    assert row["first_seen"] is None and row["last_seen"] is None


# ------------------------------------------------------------------ maintenance poller
def _three_work_orders(mocks) -> list[dict]:
    m = mocks.c("maintenance")
    return [m.post("/workorders", json={"asset": a, "opened_by": "human"}, headers=H).json()
            for a in ("1106", "1107", "1108")]


def _store(mocks):
    return mocks.apps["maintenance"].state.ctx.store


# Fix round N1 (A165) deliberately changed the bad record in the three maintenance tests below: a zone-less time with
# the flag off is now configuration (the poll holds, test_step4_fix_f1), so the per-record fault is a malformed time.
BAD = "not a time"


def test_maintenance_one_malformed_record_is_rejected_the_rest_emitted(db, mocks):
    w1, w2, w3 = _three_work_orders(mocks)
    st = _store(mocks)
    doc = st.get(AG, "wo", w2["id"])
    good_opened = doc["opened_at"]
    doc["opened_at"] = BAD                                            # this record alone carries a broken time
    st.put(AG, "wo", w2["id"], doc)
    p = MaintenancePoller(db, mocks.c("maintenance"), AG)
    p.clock = mocks.clock.now
    assert p.poll() == 2 and p.last_error is None
    assert sorted(e.data["wo_no"] for e in raw(db)) == sorted([w1["wo_no"], w3["wo_no"]])
    (row,) = list_rejects(db)
    assert (row["product"], row["native_id"], row["resolved"]) == ("maintenance", w2["id"], 0)
    assert BAD in row["reason"] and DIGEST.match(row["digest"])

    # still broken when seen again: no event, still one row, re-evaluated (last_seen moves)
    mocks.clock.advance(minutes=1)
    assert p.poll() == 0 and len(list_rejects(db)) == 1
    assert list_rejects(db)[0]["last_seen"] == mocks.clock.now().isoformat()

    # the product corrects the record (its update bumps updated_at): it flows normally, the row is resolved
    mocks.clock.advance(minutes=1)
    doc = st.get(AG, "wo", w2["id"])
    doc.update(opened_at=good_opened, updated_at=mocks.clock.iso())
    st.put(AG, "wo", w2["id"], doc)
    assert p.poll() == 1
    assert sorted(e.data["wo_no"] for e in raw(db)) == sorted([w1["wo_no"], w2["wo_no"], w3["wo_no"]])
    assert list_rejects(db) == []
    (done,) = list_rejects(db, include_resolved=True)
    assert done["resolved"] == 1 and done["resolved_at"] == mocks.clock.now().isoformat()
    assert p.status()["rejects"] == {"open": 0, "reasons": []}


def test_maintenance_close_of_an_open_work_order_with_a_bad_close_time_waits_for_the_correction(db, mocks):
    """The open was emitted earlier; the close comes with a malformed time: no WorkOrderClosed, a reject row, and the
    per-record seen state is NOT advanced, so the corrected close is still emitted (nothing lost)."""
    (w1, _, _) = _three_work_orders(mocks)
    p = MaintenancePoller(db, mocks.c("maintenance"), AG)
    p.clock = mocks.clock.now
    assert p.poll() == 3
    mocks.clock.advance(minutes=10)
    mocks.c("maintenance").post(f"/sim/workorders/{w1['id']}/close", headers=H)
    st = _store(mocks)
    doc = st.get(AG, "wo", w1["id"])
    good_closed = doc["closed_at"]
    doc["closed_at"] = BAD
    st.put(AG, "wo", w1["id"], doc)
    assert p.poll() == 0
    assert [r["native_id"] for r in list_rejects(db)] == [w1["id"]]
    mocks.clock.advance(minutes=1)
    doc.update(closed_at=good_closed, updated_at=mocks.clock.iso())
    st.put(AG, "wo", w1["id"], doc)
    assert p.poll() == 1
    assert [e.type for e in raw(db)].count("WorkOrderClosed") == 1 and list_rejects(db) == []


def test_maintenance_cursor_waits_at_an_open_reject(db, mocks):
    """Fix round N1 b (A166), deliberately reversed (was: the cursor moves past the rejected record): the cursor never
    passes the earliest open reject, so a correction that does not bump updated_at is still read."""
    m = mocks.c("maintenance")
    first = m.post("/workorders", json={"asset": "1106", "opened_by": "human"}, headers=H).json()
    p = MaintenancePoller(db, m, AG)
    assert p.poll() == 1
    _, seen = load_state(db, p.name)
    assert seen["wo_cursor"] == first["updated_at"]                   # an instant, RFC 3339 UTC
    mocks.clock.advance(minutes=5)
    late = m.post("/workorders", json={"asset": "1107", "opened_by": "human"}, headers=H).json()   # newest, and bad
    mocks.clock.advance(minutes=5)
    newer = m.post("/workorders", json={"asset": "1108", "opened_by": "human"}, headers=H).json()
    st = _store(mocks)
    doc = st.get(AG, "wo", late["id"])
    good_opened = doc["opened_at"]
    doc["opened_at"] = BAD
    st.put(AG, "wo", late["id"], doc)
    assert p.poll() == 1                                              # `newer` flows
    _, seen = load_state(db, p.name)
    assert seen["wo_cursor"] == first["updated_at"] and late["id"] not in seen["wo"]   # held at the floor
    assert seen["reject_floors"] == {"wo": {f"wo:{late['id']}": first["updated_at"]}}
    doc["opened_at"] = good_opened                                    # corrected in place, updated_at unchanged
    st.put(AG, "wo", late["id"], doc)
    assert p.poll() == 1 and list_rejects(db) == []
    _, seen = load_state(db, p.name)
    assert seen["wo_cursor"] == newer["updated_at"] and "reject_floors" not in seen


def test_an_unknown_agency_timezone_still_holds_the_whole_poll(db, mocks):
    """Configuration, not a record (A157): nothing is rejected, nothing emitted, last_error says why, and nothing is
    lost once the configuration is fixed (the A108 behaviour, unchanged)."""
    from transitos.tools.common import payload_time_reader
    mocks.c("maintenance").post("/sim/time-format", json={"local_tz": "America/New_York"}, headers=H)
    _three_work_orders(mocks)
    p = MaintenancePoller(db, mocks.c("maintenance"), AG)
    p.read_time = payload_time_reader({"local_time": True, "timezone": None})
    assert p.poll() == 0 and "timezone" in (p.last_error or "")
    assert list_rejects(db) == [] and raw(db) == []
    p.read_time = payload_time_reader({"local_time": True, "timezone": "America/New_York"})
    assert p.poll() == 3


# ------------------------------------------------------------------ portable SQL (the supervisor runs Postgres)
def test_poll_rejects_statements_translate_to_postgres():
    for stmt in split_script(wbase.REJECT_SCHEMA):
        t = translate(stmt, with_params=False)
        assert "PRAGMA" not in t.upper() and "?" not in t
    ddl = translate(split_script(wbase.REJECT_SCHEMA)[0], with_params=False)
    assert ddl.startswith("CREATE TABLE IF NOT EXISTS poll_rejects(") and ddl.endswith(", rowid BIGSERIAL)")
    assert "PRIMARY KEY(poller, record_key)" in ddl
    up = translate(wbase.REJECT_UPSERT)
    assert up.count("%s") == 9 and "ON CONFLICT(poller,record_key) DO UPDATE SET" in up and "?" not in up
    for sql in (wbase.REJECT_RESOLVE, wbase.REJECT_OPEN_KEYS, wbase.REJECT_LIST):
        assert "?" not in translate(sql) and "%s" in translate(sql)


def test_rejects_do_not_touch_the_ordinary_path(db, mocks):
    _three_work_orders(mocks)
    p = MaintenancePoller(db, mocks.c("maintenance"), AG)
    p.clock = mocks.clock.now
    assert p.poll() == 3 and list_rejects(db, include_resolved=True) == []
    mocks.clock.advance(minutes=1)
    assert p.poll() == 0


# ------------------------------------------------------------------ the integrated system: status + panel
def test_system_exposes_rejects_on_the_poller_status_and_in_the_panel():
    """End to end: the maintenance product's per-field utc_offset disagrees with the agency zone on every record (a
    per-record fault; fix round N1 deliberately replaced the flag-off repro, which now holds the poll). Every such
    record is rejected with sim seen times; the panel lists them (JSON and the unresolved page)."""
    from transitos.integration.world import make_world
    from transitos.verification.scenarios import BY_ID

    from .test_bugsweep_b2_watch import TamperOffsets
    w = make_world()
    w.apply(BY_ID["local_time_normalised"])
    proxy = TamperOffsets(w.system.pollers[0].client)
    proxy.bad.add("*")
    w.system.pollers[0].client = proxy
    w.advance_world_to("12:00")
    st = w.system.pollers[0].status()
    assert st["last_error"] is None and st["rejects"]["open"] >= 1
    items = w.system.panel_client.get("/poll-rejects", params={"agency": "agency-a"}).json()
    assert len(items) == st["rejects"]["open"] and {i["product"] for i in items} == {"maintenance"}
    assert all(i["first_seen"] and "disagree" in i["reason"] for i in items)
    page = w.system.panel_client.get("/ui/unresolved").text
    assert "Unreadable product records" in page and items[0]["native_id"] in page
    assert w.system.panel_client.get("/poll-rejects", params={"agency": "agency-zz"}).json() == []
