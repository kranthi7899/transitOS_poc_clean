"""Track B watch adapters + crosswalk against Track A's REAL mock products (not B's local fakes)."""
from __future__ import annotations

import pytest

from transitos.contracts.envelope import TOPIC_RAW, TOPIC_RESOLVED
from transitos.contracts.events import DATA_MODELS
from transitos.mocks.crosswalk_seed import crosswalk_entries
from transitos.plumbing import CrosswalkService, Db, Relay, SqliteBus, SqliteCrosswalk
from transitos.plumbing.watch import (
    MaintenancePoller,
    SchedulingPoller,
    TelematicsBackfillPoller,
    WebhookReceiver,
    WorkforcePoller,
)

from .conftest import AG, H


@pytest.fixture
def db(tmp_path):
    return Db(str(tmp_path / "b.db"))


def raw(db):
    bus = SqliteBus(db)
    Relay(db, bus).drain()
    return [e for _, _, e in bus.messages(TOPIC_RAW)]


def test_maintenance_pm_overdue_once_despite_inclusive_filter(db, mocks):
    p = MaintenancePoller(db, mocks.c("maintenance"), AG)
    assert p.poll() == 0                                    # seed: nothing overdue
    mocks.c("maintenance").post("/sim/meter", json={"asset": "1108", "meter_mi": 25100}, headers=H)
    assert p.poll() == 1
    assert p.poll() == 0 and p.poll() == 0                  # updated_since is inclusive (>=): re-read, deduped
    (ev,) = raw(db)
    assert ev.type == "PMOverdue" and ev.data["native_asset_id"] == "1108" and ev.data["meter_mi"] == 25100
    DATA_MODELS[ev.type].model_validate(ev.data)


def test_maintenance_open_close_and_cancel_is_not_a_repair(db, mocks):
    m = mocks.c("maintenance")
    p = MaintenancePoller(db, m, AG)
    a = m.post("/workorders", json={"asset": "1106", "description": "x", "opened_by": "system"},
               headers={**H, "Idempotency-Key": "k1"}).json()
    b = m.post("/workorders", json={"asset": "1107", "description": "y", "opened_by": "system"},
               headers={**H, "Idempotency-Key": "k2"}).json()
    p.poll()
    m.post(f"/sim/workorders/{a['id']}/close", headers=H)       # shop closes WO a (a real repair)
    m.post(f"/workorders/{b['id']}/cancel", headers=H)          # our compensation cancels WO b
    p.poll()
    p.poll()
    evs = raw(db)
    assert [(e.type, e.data["wo_no"]) for e in evs] == [
        ("WorkOrderOpened", a["wo_no"]), ("WorkOrderOpened", b["wo_no"]), ("WorkOrderClosed", a["wo_no"])]
    assert evs[0].data["opened_by"] == "system"


def test_maintenance_first_seen_cancelled_wo_emits_no_close(db, mocks):
    m = mocks.c("maintenance")
    b = m.post("/workorders", json={"asset": "1107", "description": "y", "opened_by": "system"}, headers=H).json()
    m.post(f"/workorders/{b['id']}/cancel", headers=H)
    MaintenancePoller(db, m, AG).poll()
    assert [e.type for e in raw(db)] == ["WorkOrderOpened"]


def test_scheduling_status_changes_carry_actor_and_dedupe(db, mocks):
    s = mocks.c("scheduling")
    p = SchedulingPoller(db, s, AG)
    s.post("/sim/vehicle-status", json={"vehicle": "BUS-53", "status": "reserved", "changed_by": "dispatcher"}, headers=H)
    s.post("/vehicles/BUS-44/unavailable", json={"start": None, "end": None, "changed_by": "transitos:r:k:1"},
           headers=H)
    assert p.poll() == 2 and p.poll() == 0
    evs = raw(db)
    assert [(e.data["native_vehicle_id"], e.data["status"], e.data["changed_by"]) for e in evs] == [
        ("BUS-53", "reserved", "dispatcher"), ("BUS-44", "out_of_service", "transitos:r:k:1")]
    for e in evs:
        DATA_MODELS[e.type].model_validate(e.data)


def test_workforce_availability_item_shape(db, mocks):
    w = mocks.c("workforce")
    p = WorkforcePoller(db, w, AG)
    w.post("/sim/operator", json={"operator": "OP-312", "status": "unavailable"}, headers=H)
    assert p.poll() == 1 and p.poll() == 0
    (ev,) = raw(db)
    assert ev.type == "OperatorAvailabilityChanged"
    assert ev.data == {"native_operator_id": "OP-312", "status": "unavailable"}


def _webhook(mocks, db):
    rx = WebhookReceiver(db, AG)
    out = []
    mocks.sender = lambda url, body, headers: out.append(rx.handle(body, headers["X-Signature"]))
    mocks.c("telematics").post("/sim/webhook", json={"url": "http://receiver/webhook"}, headers=H)
    return out


def test_telematics_fault_webhook_signed_by_mock_is_accepted(db, mocks):
    out = _webhook(mocks, db)
    mocks.c("telematics").post("/sim/fault", json={"vehicle": "T-77", "spn": 520, "fmi": 3, "lamps": {"stop": True}},
                               headers=H)
    assert out == [(200, {"status": "accepted"})]
    (ev,) = raw(db)
    assert ev.type == "VehicleFaultDetected" and ev.data["native_vehicle_id"] == "T-77"
    assert ev.data["lamps"]["stop"] is True and ev.data["occurrence_count"] == 1


def test_telematics_dvir_shape_from_mock(db, mocks):
    out = _webhook(mocks, db)
    mocks.c("telematics").post("/dvir", json={"vehicle": "T-77", "defect_text": "brake pedal soft",
                                              "reported_by_role": "operator"}, headers=H)
    assert out == [(200, {"status": "accepted"})]
    (ev,) = raw(db)
    assert ev.type == "DefectReported" and ev.data["defect_text"] == "brake pedal soft"
    assert ev.data["reported_by_role"] == "operator"
    DATA_MODELS[ev.type].model_validate(ev.data)


def test_telematics_duplicate_and_backfill_reconcile(db, mocks):
    out = _webhook(mocks, db)
    mocks.chaos["telematics"].set("duplicate")
    mocks.c("telematics").post("/sim/fault", json={"vehicle": "T-78", "spn": 1, "fmi": 1}, headers=H)
    assert [s for _, s in out] == [{"status": "accepted"}, {"status": "duplicate"}]
    assert TelematicsBackfillPoller(db, mocks.c("telematics"), AG).poll() == 0   # feed has it twice too
    assert len(raw(db)) == 1


def test_telematics_late_event_arrives_via_backfill_after_clock_advance(db, mocks):
    mocks.chaos["telematics"].set("late")
    mocks.c("telematics").post("/sim/fault", json={"vehicle": "T-79", "spn": 2, "fmi": 1}, headers=H)
    bf = TelematicsBackfillPoller(db, mocks.c("telematics"), AG)
    assert bf.poll() == 0
    mocks.clock.advance(minutes=31)
    assert bf.poll() == 1 and bf.poll() == 0


def test_raw_to_resolved_with_mock_crosswalk_seed(db, mocks):
    bus = SqliteBus(db)
    xw = SqliteCrosswalk(db, bus)
    for e in crosswalk_entries(AG):
        xw.resolve(e)
    _webhook(mocks, db)
    mocks.c("telematics").post("/sim/fault", json={"vehicle": "T-77", "spn": 1, "fmi": 1}, headers=H)
    mocks.c("maintenance").post("/sim/meter", json={"asset": "1108", "meter_mi": 25100}, headers=H)
    MaintenancePoller(db, mocks.c("maintenance"), AG).poll()
    Relay(db, bus).drain()
    CrosswalkService(db, bus, xw).drain()
    res = {e.type: e for _, _, e in bus.messages(TOPIC_RESOLVED)}
    assert res["VehicleFaultDetected"].data["refs"]["canonical"] == "veh-1"
    assert res["VehicleFaultDetected"].canonicalid == "veh-1"
    assert res["PMOverdue"].data["refs"] == {"canonical": "veh-3", "telematics": "T-79", "maintenance": "1108",
                                             "scheduling": "BUS-46"}
