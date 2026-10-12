"""Bug sweep B fix round (R4), through the REAL pollers and the REAL mock products. Expected rows from the ruling text.

N1 (A165, A166). (a) A zone-less product time while the agency's local_time flag is OFF is CONFIGURATION (the whole
product sends local time): the poll HOLDS and retries, nothing is rejected, and once the owner sets local_time every
record (open AND close) flows. Per-record rejects stay only for genuinely per-record faults (a utc_offset that
disagrees with the zone, a bad or out-of-range epoch). (b) Cursors compare as INSTANTS, never as strings, and never
advance past the earliest OPEN reject of their stream, so a rejected record that is later corrected is always re-read.
N2 (A167). A NUL in a vendor id never reaches a TEXT column (Postgres refuses it and would abort the poll)."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from transitos.contracts.envelope import TOPIC_RAW
from transitos.plumbing import Db, Relay, SqliteBus
from transitos.plumbing.watch import MaintenancePoller, TelematicsBackfillPoller
from transitos.plumbing.watch.base import list_rejects, load_state, zoned
from transitos.tools.common import payload_time_reader

from .conftest import AG, H

ZONES = ["Asia/Kolkata", "America/New_York"]          # east and west of UTC


@pytest.fixture
def db(tmp_path):
    return Db(str(tmp_path / "b.db"))


def raw(db):
    bus = SqliteBus(db)
    Relay(db, bus).drain()
    return [e for _, _, e in bus.messages(TOPIC_RAW)]


def live_reader(cfg: dict):
    """The System's maintenance time reader (integration/system.py `_maintenance_time`), reading `cfg` live."""
    def read(doc: dict, key: str) -> Any:
        r = payload_time_reader(cfg)
        return zoned(doc[key]) if r is None else r(doc, key)
    return read


def utc(s: str) -> datetime:
    return datetime.fromisoformat(s)


# ------------------------------------------------------------------ N1 (a): the reviewer's test_q2, both zones
@pytest.mark.parametrize("tz", ZONES)
def test_flag_off_holds_then_flag_on_emits_open_and_close_of_a_and_open_of_b(db, mocks, tz):
    m = mocks.c("maintenance")
    m.post("/sim/time-format", json={"local_tz": tz, "utc_offset": True}, headers=H)   # the product sends local time
    a = m.post("/workorders", json={"asset": "1106", "opened_by": "human"}, headers=H).json()
    t_open_a = mocks.clock.now()
    mocks.clock.advance(minutes=60)
    m.post(f"/sim/workorders/{a['id']}/close", headers=H)
    t_close_a = mocks.clock.now()
    mocks.clock.advance(minutes=30)
    b = m.post("/workorders", json={"asset": "1107", "opened_by": "human"}, headers=H).json()
    t_open_b = mocks.clock.now()
    cfg = {"local_time": False, "timezone": tz}                       # ... but the agency flag is off
    p = MaintenancePoller(db, m, AG)
    p.read_time, p.clock = live_reader(cfg), mocks.clock.now
    for _ in range(10):
        assert p.poll() == 0
        assert "zone" in (p.last_error or "")                         # held, with the reason
    assert list_rejects(db, include_resolved=True) == []              # configuration: no record is rejected
    assert raw(db) == []
    cfg["local_time"] = True                                          # the owner fixes the configuration
    assert p.poll() == 3 and p.last_error is None
    got = sorted((e.type, e.data["wo_no"], e.time) for e in raw(db))
    assert got == sorted([("WorkOrderOpened", a["wo_no"], t_open_a), ("WorkOrderClosed", a["wo_no"], t_close_a),
                          ("WorkOrderOpened", b["wo_no"], t_open_b)])
    assert list_rejects(db, include_resolved=True) == [] and p.poll() == 0


@pytest.mark.parametrize("tz", ZONES)
def test_the_system_flag_off_then_on_no_run_stays_stuck(tz):
    """End to end on the integrated system: the products are local, the agency flag is off for a while, then on; the
    work order's open and close both reach the bus and no reject row is left open."""
    from transitos.integration.world import make_world
    from transitos.verification.scenarios import BY_ID
    w = make_world()
    w.apply(BY_ID["local_time_normalised"])
    s = w.system
    for prod in ("maintenance",):
        s.mocks.clients[prod].post("/sim/time-format", json={"local_tz": tz, "utc_offset": True},
                                   headers={"X-Agency": "agency-a"})
    s.registry_settings["agency-a"].update(local_time=False, timezone=tz)
    w.advance_world_to("12:00")
    assert "zone" in (s.pollers[0].last_error or "")
    s.registry_settings["agency-a"]["local_time"] = True
    s.run_until_idle()
    types = [e.type for _, _, e in s.bus.messages(TOPIC_RAW) if e.source.startswith("adapter/maintenance")]
    assert "WorkOrderOpened" in types and "WorkOrderClosed" in types
    assert s.panel_client.get("/poll-rejects", params={"agency": "agency-a"}).json() == []
    assert s.pollers[0].last_error is None


# ------------------------------------------------------------------ N1 (b): cursors as instants, floor at open rejects
class TamperOffsets:
    """The maintenance client with ONE record's utc_offset made to disagree with the zone while `bad` holds it (a
    genuinely per-record fault). Everything else is the real mock."""

    def __init__(self, inner: Any) -> None:
        self.inner, self.bad = inner, set()

    def get(self, path: str, **kw: Any):
        r = self.inner.get(path, **kw)
        if path == "/workorders" and self.bad:
            import httpx
            docs = r.json()
            for d in docs:
                if d["id"] in self.bad or "*" in self.bad:
                    d["utc_offset"] = {k: "+09:00" for k in d.get("utc_offset", {})}
            return httpx.Response(200, json=docs, request=httpx.Request("GET", path))
        return r

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


@pytest.mark.parametrize("tz", ZONES)
def test_a_corrected_record_is_re_read_even_without_an_update(db, mocks, tz):
    m = mocks.c("maintenance")
    m.post("/sim/time-format", json={"local_tz": tz, "utc_offset": True}, headers=H)
    a = m.post("/workorders", json={"asset": "1106", "opened_by": "human"}, headers=H).json()
    mocks.clock.advance(minutes=30)
    b = m.post("/workorders", json={"asset": "1107", "opened_by": "human"}, headers=H).json()
    t_b = mocks.clock.now()
    proxy = TamperOffsets(m)
    proxy.bad.add(a["id"])
    p = MaintenancePoller(db, proxy, AG)
    p.read_time, p.clock = live_reader({"local_time": True, "timezone": tz}), mocks.clock.now
    assert p.poll() == 1 and p.last_error is None                    # B flows, A is a per-record reject
    (row,) = list_rejects(db)
    assert row["native_id"] == a["id"] and "disagree" in row["reason"]
    _, seen = load_state(db, p.name)
    assert seen["wo_cursor"] is None                                  # never past the open reject (it came at since=None)
    mocks.clock.advance(minutes=5)
    assert p.poll() == 0 and len(list_rejects(db)) == 1               # still bad: re-read, still one row
    proxy.bad.clear()                                                 # the product corrects A, no new updated_at
    assert p.poll() == 1
    assert sorted(e.data["wo_no"] for e in raw(db)) == sorted([a["wo_no"], b["wo_no"]])
    assert list_rejects(db) == [] and list_rejects(db, include_resolved=True)[0]["resolved"] == 1
    _, seen = load_state(db, p.name)
    assert utc(seen["wo_cursor"]) == t_b                              # the reject resolved: the cursor moves on


@pytest.mark.parametrize(("tz", "local_b"), [("Asia/Kolkata", "2026-10-10T16:30:00"),
                                             ("America/New_York", "2026-10-10T07:00:00")])
def test_cursor_compares_instants_when_the_wire_changes_from_utc_to_local(db, mocks, tz, local_b):
    """The product first sends UTC, then switches to local time (and the agency flag follows). The saved cursor is the
    newest INSTANT (11:00Z), whatever the strings sort as (east: '16:30:00' > '10:00:00Z'; west: '07:00:00' <)."""
    m = mocks.c("maintenance")
    mocks.clock.advance(hours=(datetime(2026, 10, 10, 10, tzinfo=UTC) - mocks.clock.now()).total_seconds() / 3600)
    m.post("/workorders", json={"asset": "1106", "opened_by": "human"}, headers=H)       # 10:00Z, UTC wire
    cfg = {"local_time": False, "timezone": tz}
    p = MaintenancePoller(db, m, AG)
    p.read_time = live_reader(cfg)
    assert p.poll() == 1
    m.post("/sim/time-format", json={"local_tz": tz, "utc_offset": True}, headers=H)
    cfg["local_time"] = True
    mocks.clock.advance(minutes=60)
    b = m.post("/workorders", json={"asset": "1107", "opened_by": "human"}, headers=H).json()   # 11:00Z
    assert b["updated_at"] == local_b
    assert p.poll() == 1
    _, seen = load_state(db, p.name)
    assert utc(seen["wo_cursor"]) == datetime(2026, 10, 10, 11, tzinfo=UTC)
    assert p.poll() == 0 and len(raw(db)) == 2


def test_telematics_cursor_waits_at_an_open_reject_and_the_corrected_item_flows(db, mocks):
    t = mocks.c("telematics")
    good = t.post("/sim/fault", json={"vehicle": "T-77", "spn": 110, "fmi": 0}, headers=H).json()
    bad = t.post("/sim/fault", json={"vehicle": "T-78", "spn": 110, "fmi": 0}, headers=H).json()
    st = mocks.apps["telematics"].state.ctx.store
    fixed_time = bad["time"]
    for e in st.all(AG, "events"):
        if e["payload"]["eventId"] == bad["eventId"]:
            e["payload"]["time"] = 0                                  # out-of-range epoch: per-record (A146)
            st.put(AG, "events", f"{e['seq']:08d}", e)
    p = TelematicsBackfillPoller(db, t, AG)
    assert p.poll() == 1 and len(list_rejects(db)) == 1
    assert load_state(db, p.name)[0] is None                          # not past the open reject
    for e in st.all(AG, "events"):
        if e["payload"]["eventId"] == bad["eventId"]:
            e["payload"]["time"] = fixed_time                         # the vendor corrects the item in place
            st.put(AG, "events", f"{e['seq']:08d}", e)
    assert p.poll() == 1 and list_rejects(db) == []
    assert sorted(e.data["native_vehicle_id"] for e in raw(db)) == ["T-77", "T-78"]
    assert load_state(db, p.name)[0] == t.get("/faults/feed", headers=H).json()["next_cursor"]
    assert good["eventId"] != bad["eventId"]


# ------------------------------------------------------------------ N2: NUL in a vendor id
def test_a_nul_in_a_vendor_event_id_never_reaches_the_table(db, mocks):
    t = mocks.c("telematics")
    t.post("/sim/fault", json={"vehicle": "T-77", "spn": 110, "fmi": 0}, headers=H)
    st = mocks.apps["telematics"].state.ctx.store
    for e in st.all(AG, "events"):
        e["payload"]["eventId"] = "evt\x00-1"
        e["payload"]["time"] = 0
        st.put(AG, "events", f"{e['seq']:08d}", e)
    p = TelematicsBackfillPoller(db, t, AG)
    assert p.poll() == 0
    (row,) = list_rejects(db)
    assert "\x00" not in row["native_id"] and "\x00" not in row["record_key"] and "\x00" not in row["reason"]
    assert row["native_id"] == "evt\\0-1" and "NUL" in row["reason"]
    assert p.poll() == 0 and len(list_rejects(db)) == 1               # the same record maps to the same row
