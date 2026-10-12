"""Phase 10 Step 4 (Track B) against the REAL maintenance and telematics mocks: the shop vocabulary never reads as a
close except CLOSED, and product times reach the bus as UTC (zone-less local via the agency timezone, epoch ms)."""
from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from transitos.contracts.envelope import TOPIC_RAW
from transitos.plumbing import Db, Relay, SqliteBus
from transitos.plumbing.watch import MaintenancePoller, WebhookReceiver
from transitos.plumbing.watch.telematics import ingest_payload
from transitos.tools.common import payload_time_reader

from .conftest import AG, H

NY = "America/New_York"


@pytest.fixture
def db(tmp_path):
    return Db(str(tmp_path / "b.db"))


def raw(db):
    bus = SqliteBus(db)
    Relay(db, bus).drain()
    return [e for _, _, e in bus.messages(TOPIC_RAW)]


def converted(m) -> dict:
    pr = m.post("/pending-repairs", json={"asset": "1106", "requested_by": "system"},
                headers={**H, "Idempotency-Key": "k"}).json()
    return m.post(f"/sim/pending-repairs/{pr['id']}/convert", headers=H).json()["work_order"]


@pytest.mark.parametrize("status", ["IN_PROGRESS", "WAITING_PARTS", "PENDING", "COMPLETE"])
def test_no_work_order_closed_for_a_shop_status_that_is_not_closed(db, mocks, status):
    m = mocks.c("maintenance")
    p = MaintenancePoller(db, m, AG)
    wo = converted(m)
    p.poll()
    m.post(f"/sim/workorders/{wo['id']}/status", json={"status": status}, headers=H)
    p.poll()
    p.poll()
    assert [e.type for e in raw(db)] == ["WorkOrderOpened"]


def test_closed_emits_work_order_closed_once_after_a_parts_wait_and_a_deferral(db, mocks):
    m = mocks.c("maintenance")
    p = MaintenancePoller(db, m, AG)
    wo = converted(m)
    for s in ("IN_PROGRESS", "WAITING_PARTS", "IN_PROGRESS", "PENDING", "IN_PROGRESS", "COMPLETE"):
        m.post(f"/sim/workorders/{wo['id']}/status", json={"status": s}, headers=H)
        p.poll()
    m.post(f"/sim/workorders/{wo['id']}/close", headers=H)
    p.poll()
    p.poll()
    assert [e.type for e in raw(db)] == ["WorkOrderOpened", "WorkOrderClosed"]


def test_default_poller_passes_product_times_through_unchanged(db, mocks):
    m = mocks.c("maintenance")
    p = MaintenancePoller(db, m, AG)
    m.post("/workorders", json={"asset": "1106", "opened_by": "human"}, headers=H)
    p.poll()
    (ev,) = raw(db)
    assert ev.time == mocks.clock.now()


def _to(mocks, t: datetime) -> None:
    mocks.clock.advance(hours=(t - mocks.clock.now()).total_seconds() / 3600)
    assert mocks.clock.now() == t


def test_fall_back_close_with_an_earlier_local_wall_time_is_seen_and_stamped_in_utc(db, mocks):
    """Opened 05:41Z (01:41 EDT), closed 06:30Z (01:30 EST): the close's local time sorts BEFORE the open's. The
    cursor must not skip it and the event must carry 06:30Z (the product's utc_offset disambiguates)."""
    m = mocks.c("maintenance")
    m.post("/sim/time-format", json={"local_tz": NY, "utc_offset": True}, headers=H)
    p = MaintenancePoller(db, m, AG)
    p.read_time = payload_time_reader({"local_time": True, "timezone": NY})
    _to(mocks, datetime(2026, 11, 1, 5, 41, tzinfo=UTC))
    wo = m.post("/workorders", json={"asset": "1106", "opened_by": "system"}, headers=H).json()
    assert wo["opened_at"] == "2026-11-01T01:41:00"
    p.poll()
    _to(mocks, datetime(2026, 11, 1, 6, 30, tzinfo=UTC))
    closed = m.post(f"/sim/workorders/{wo['id']}/close", headers=H).json()
    assert closed["closed_at"] == "2026-11-01T01:30:00"
    p.poll()
    evs = raw(db)
    assert [(e.type, e.time) for e in evs] == [
        ("WorkOrderOpened", datetime(2026, 11, 1, 5, 41, tzinfo=UTC)),
        ("WorkOrderClosed", datetime(2026, 11, 1, 6, 30, tzinfo=UTC))]
    assert all(e.time.tzinfo is not None for e in evs)


def test_fall_back_without_offsets_follows_the_first_occurrence_rule(db, mocks):
    m = mocks.c("maintenance")
    m.post("/sim/time-format", json={"local_tz": NY}, headers=H)
    p = MaintenancePoller(db, m, AG)
    p.read_time = payload_time_reader({"local_time": True, "timezone": NY})
    _to(mocks, datetime(2026, 11, 1, 6, 30, tzinfo=UTC))
    m.post("/workorders", json={"asset": "1106", "opened_by": "human"}, headers=H)
    p.poll()
    (ev,) = raw(db)
    assert ev.time == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)   # documented rule (A109): an hour early, by design


def test_unknown_timezone_holds_the_poll_and_loses_nothing(db, mocks):
    m = mocks.c("maintenance")
    m.post("/sim/time-format", json={"local_tz": NY}, headers=H)
    p = MaintenancePoller(db, m, AG)
    p.read_time = payload_time_reader({"local_time": True, "timezone": None})
    m.post("/workorders", json={"asset": "1106", "opened_by": "human"}, headers=H)
    assert p.poll() == 0 and "timezone" in (p.last_error or "")
    assert raw(db) == []                                         # no event with a guessed time
    p.read_time = payload_time_reader({"local_time": True, "timezone": NY})   # config fixed
    assert p.poll() == 1
    assert raw(db)[0].time == mocks.clock.now()


def test_settings_without_local_time_give_no_reader():
    assert payload_time_reader({}) is None and payload_time_reader({"timezone": NY}) is None


# ------------------------------------------------------------------ telematics epoch milliseconds (A110)
def test_epoch_ms_and_rfc3339_give_the_same_event_id_and_utc_time(db, mocks):
    t = mocks.c("telematics")
    t.post("/sim/time-format", json={"epoch_ms": True}, headers=H)
    p_ms = t.post("/sim/fault", json={"vehicle": "T-77", "spn": 110, "fmi": 0}, headers=H).json()
    assert isinstance(p_ms["time"], int)
    p_iso = {**p_ms, "eventId": "evt-iso", "time": mocks.clock.iso()}
    with db.tx() as c:
        assert ingest_payload(c, AG, p_ms)
    with db.tx() as c2:
        assert not ingest_payload(c2, AG, p_iso)      # same fault at the same instant: same event id, deduped
    (ev,) = raw(db)
    assert ev.time == mocks.clock.now() and ev.time.tzinfo is not None


def test_webhook_rejects_a_zone_less_telematics_time(db):
    rec = WebhookReceiver(db, AG)
    body = json.dumps({"eventId": "e1", "vehicle": {"id": "T-77"}, "fault": {"spn": 110, "fmi": 0},
                       "time": "2026-11-01T01:30:00"}).encode()
    from transitos.plumbing.watch.telematics import sign
    code, out = rec.handle(body, sign("dev-secret", body))
    assert code == 400 and "zone" in out["error"]
