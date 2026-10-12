"""Step 4 fix round F1: with the agency's local_time flag OFF, a zone-less product time is refused by the watch
adapters too (same rule as the read adapters' _zoned_dt): it never emits a naive CloudEvent.time that a later stage
reads as UTC. And no event on events.raw or events.resolved is ever naive.

Bug sweep B (F7-R4, A155) DELIBERATELY changed the expectation of the first, second and last tests here: the poll no
longer HOLDS on such a record. That record is REJECTED (a poll_rejects row naming the zone problem, never emitted)
and the rest of the page is emitted; last_error keeps its meaning (the poll itself failed or is held), so it stays
None. An unknown agency TIMEZONE still holds the poll (test_step4_watch, A157)."""
from __future__ import annotations

import pytest

from transitos.contracts.envelope import TOPIC_RAW, TOPIC_RESOLVED
from transitos.integration.world import make_world
from transitos.plumbing import Db, Relay, SqliteBus
from transitos.plumbing.watch import (
    MaintenancePoller,
    SchedulingPoller,
    WorkforcePoller,
)
from transitos.plumbing.watch.base import list_rejects
from transitos.tools.common import payload_time_reader
from transitos.verification.runner import drive
from transitos.verification.scenarios import BY_ID, SCENARIOS

from .conftest import AG, H

NY = "America/New_York"


@pytest.fixture
def db(tmp_path):
    return Db(str(tmp_path / "b.db"))


def raw(db):
    bus = SqliteBus(db)
    Relay(db, bus).drain()
    return [e for _, _, e in bus.messages(TOPIC_RAW)]


@pytest.mark.parametrize("reader", ["none", "flag_off"])
def test_reviewer_repro_zone_less_maintenance_time_with_the_flag_off_is_rejected_not_emitted(db, mocks, reader):
    m = mocks.c("maintenance")
    m.post("/sim/time-format", json={"local_tz": NY}, headers=H)          # the product sends local time ...
    p = MaintenancePoller(db, m, AG)
    if reader == "flag_off":                                              # ... the agency config does not say so
        p.read_time = payload_time_reader({"local_time": False, "timezone": NY})
    wo = m.post("/workorders", json={"asset": "1106", "opened_by": "human"}, headers=H).json()
    assert p.poll() == 0
    assert raw(db) == []                                                  # never a naive (= UTC-guessed) event
    # bug sweep B (A155): the record is rejected, not held: one reject row, last_error untouched
    (row,) = list_rejects(db)
    assert row["native_id"] == wo["id"] and "zone" in row["reason"] and p.last_error is None


def test_scheduling_and_workforce_pollers_reject_a_zone_less_change_time_and_emit_the_rest(db, mocks):
    st = mocks.apps["scheduling"].state.ctx.store
    wf = mocks.apps["workforce"].state.ctx.store
    st.put(AG, "changes", "zz", {"vehicle": "BUS-44", "status": "out_of_service", "changed_at": "2026-01-05T07:00:00",
                                "changed_by": "x"})
    st.put(AG, "changes", "zzz", {"vehicle": "BUS-45", "status": "out_of_service",
                                  "changed_at": "2026-01-05T07:00:00Z", "changed_by": "x"})
    wf.put(AG, "changes", "zz", {"operator": "OP-310", "status": "unavailable", "changed_at": "2026-01-05T07:00:00"})
    wf.put(AG, "changes", "zzz", {"operator": "OP-311", "status": "unavailable", "changed_at": "2026-01-05T07:00:00Z"})
    for P, product in ((SchedulingPoller, "scheduling"), (WorkforcePoller, "workforce")):
        p = P(db, mocks.c(product), AG)
        assert p.poll() == 1 and p.last_error is None, product           # the good change only
        (row,) = list_rejects(db, poller=p.name)
        assert row["native_id"] in ("BUS-44", "OP-310") and "zone" in row["reason"], product
    evs = raw(db)
    assert sorted(e.data.get("native_vehicle_id") or e.data["native_operator_id"] for e in evs) == ["BUS-45", "OP-311"]
    assert all(e.time.tzinfo is not None for e in evs)


def test_the_default_utc_wire_is_unchanged(db, mocks):
    m = mocks.c("maintenance")
    p = MaintenancePoller(db, m, AG)
    m.post("/workorders", json={"asset": "1106", "opened_by": "human"}, headers=H)
    assert p.poll() == 1
    (ev,) = raw(db)
    assert ev.time == mocks.clock.now() and ev.time.tzinfo is not None


def bus_times(w) -> list:
    return [(topic, e.type, e.time) for topic in (TOPIC_RAW, TOPIC_RESOLVED) for _, _, e in w.system.bus.messages(topic)]


@pytest.mark.parametrize("sid", [s.id for s in SCENARIOS if getattr(s.tweaks, "world_script", None) is not None])
def test_no_event_on_either_bus_topic_is_naive(sid):
    """Walk events.raw and events.resolved after every world-engine scenario (knob on and off)."""
    w = make_world()
    drive(w, BY_ID[sid])
    times = bus_times(w)
    assert times and {t for t, _, _ in times} == {TOPIC_RAW, TOPIC_RESOLVED}
    assert [x for x in times if x[2].tzinfo is None or x[2].utcoffset() is None] == []


def test_products_on_local_time_with_the_agency_flag_off_never_reach_the_bus():
    """End to end: the products send zone-less local time, the agency's flag is off. Nothing from maintenance is
    published with a guessed time; each such record is rejected with a clear reason (bug sweep B, A155)."""
    sc = BY_ID["local_time_normalised"]
    w = make_world()
    w.apply(sc)
    w.system.registry_settings["agency-a"]["local_time"] = False
    w.advance_world_to("12:00")
    maint = [e for _, _, e in w.system.bus.messages(TOPIC_RAW) if e.source.startswith("adapter/maintenance")]
    assert maint == []
    st = w.system.pollers[0].status()
    assert st["last_error"] is None and st["rejects"]["open"] >= 1
    assert all("zone" in r for r in st["rejects"]["reasons"])
