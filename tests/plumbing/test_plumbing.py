import json

import pytest

from transitos.contracts.crosswalk import CrosswalkEntry
from transitos.contracts.envelope import (
    TOPIC_FAILED,
    TOPIC_RAW,
    TOPIC_RESOLVED,
    CloudEvent,
)
from transitos.contracts.objects import DecisionRecord
from transitos.plumbing import AuditConsumer, DecisionStore, KafkaBus
from transitos.plumbing.watch import (
    MaintenancePoller,
    SchedulingPoller,
    TelematicsBackfillPoller,
    WebhookReceiver,
    WorkforcePoller,
    sign,
)

from . import fakes
from .conftest import AG, client


def ev(i, type_="VehicleFaultDetected", **data):
    d = {"native_vehicle_id": "T-77", "spn": 1, "fmi": 1}
    d.update(data)
    return CloudEvent(id=i, source=f"adapter/telematics/{AG}", type=type_, time="2026-01-01T00:00:00Z",
                      agencyid=AG, data=d)


def count_outbox(db):
    return db.execute("SELECT count(*) c FROM outbox").fetchone()["c"]


# ---- bus ------------------------------------------------------------------------------------
def test_bus_offsets_replay_and_at_least_once(bus):
    for i in range(3):
        bus.send("t", "k", ev(f"e{i}"))
    first = list(bus.poll("t", "g"))
    assert [e.id for _, e in first] == ["e0", "e1", "e2"]
    assert len(list(bus.poll("t", "g"))) == 3  # no commit -> redelivered
    bus.commit("t", "g", first[1][0])
    assert [e.id for _, e in bus.poll("t", "g")] == ["e2"]
    assert len(list(bus.poll("t", "other"))) == 3  # independent groups
    bus.reset_group("t", "g", 0)
    assert len(list(bus.poll("t", "g"))) == 3  # replay from 0


def test_kafka_bus_without_optional_dependency_fails_clearly(monkeypatch):
    from transitos.plumbing import kafka_bus
    monkeypatch.setattr(kafka_bus, "_IMPORT_ERROR", ImportError("No module named 'confluent_kafka'"))
    with pytest.raises(ImportError, match="uv sync --extra kafka"):
        KafkaBus()


# ---- adapters -------------------------------------------------------------------------------
def maint(db, state):
    return MaintenancePoller(db, client(fakes.maintenance_app(state)), AG)


def test_maintenance_events(db):
    state = {"pm": [{"asset": "1106", "service_code": "A", "due_mi": 5000, "meter_mi": 4000, "updated_at": "2026-01-01T00:00:00Z"}],
             "wos": []}
    p = maint(db, state)
    assert p.poll() == 0  # not overdue yet
    state["pm"] = [{"asset": "1106", "service_code": "A", "due_mi": 5000, "meter_mi": 5100, "updated_at": "2026-01-02T00:00:00Z"}]
    state["wos"] = [{"id": "w1", "wo_no": "WO-1", "asset": "1106", "status": "open", "opened_by": "system",
                     "opened_at": "2026-01-02T01:00:00Z", "closed_at": None, "updated_at": "2026-01-02T01:00:00Z"}]
    assert p.poll() == 2
    state["wos"][0].update(status="closed", closed_at="2026-01-03T00:00:00Z", updated_at="2026-01-03T00:00:00Z")
    assert p.poll() == 1
    assert p.poll() == 0
    types = [json.loads(r["payload"])["type"] for r in db.execute("SELECT payload FROM outbox ORDER BY seq")]
    assert types == ["PMOverdue", "WorkOrderOpened", "WorkOrderClosed"]


def test_scheduling_and_workforce(db):
    s, w = {"changes": [{"vehicle": "BUS-44", "status": "out_of_service", "changed_at": "2026-01-01T00:00:00Z", "changed_by": "x"}]}, \
           {"changes": [{"operator": "OP-310", "status": "unavailable", "changed_at": "2026-01-01T00:00:00Z"}]}
    assert SchedulingPoller(db, client(fakes.scheduling_app(s)), AG).poll() == 1
    assert WorkforcePoller(db, client(fakes.workforce_app(w)), AG).poll() == 1


def test_crash_mid_poll_loses_and_duplicates_nothing(db):
    state = {"changes": [{"vehicle": f"BUS-{i}", "status": "ready", "changed_at": f"2026-01-01T00:00:0{i}Z", "changed_by": ""}
                         for i in range(3)]}
    p = SchedulingPoller(db, client(fakes.scheduling_app(state)), AG)

    def boom():
        raise RuntimeError("crash")

    p.before_commit = boom
    with pytest.raises(RuntimeError):
        p.poll()
    assert count_outbox(db) == 0  # rolled back, cursor not advanced
    p.before_commit = None
    assert p.poll() == 3
    assert count_outbox(db) == 3
    # crash AFTER commit equivalent: a fresh poller with same state re-reads nothing new
    p2 = SchedulingPoller(db, client(fakes.scheduling_app(state)), AG)
    assert p2.poll() == 0 and count_outbox(db) == 3


def test_repeated_cursor_reread_is_deduped_by_outbox(db):
    state = {"changes": [{"vehicle": "BUS-1", "status": "ready", "changed_at": "2026-01-01T00:00:00Z", "changed_by": ""}]}
    p = SchedulingPoller(db, client(fakes.scheduling_app(state)), AG)
    p.poll()
    db.execute("DELETE FROM adapter_state")  # lost cursor -> full re-read
    assert p.poll() == 0 and count_outbox(db) == 1


def test_upstream_error_keeps_state(db):
    import httpx

    def handler(request):
        return httpx.Response(429)

    p = SchedulingPoller(db, httpx.Client(transport=httpx.MockTransport(handler), base_url="http://m"), AG)
    assert p.poll() == 0 and p.last_error


# ---- telematics -------------------------------------------------------------------------------
def test_webhook_hmac_reject_and_duplicate(db):
    rx = WebhookReceiver(db, AG, "dev-secret")
    body = json.dumps(fakes.fault_payload()).encode()
    assert rx.handle(body, None)[0] == 401
    assert rx.handle(body, "sha256=deadbeef")[0] == 401
    assert rx.handle(body, sign("wrong", body))[0] == 401
    assert count_outbox(db) == 0
    assert rx.handle(body, sign("dev-secret", body)) == (200, {"status": "accepted"})
    assert rx.handle(body, sign("dev-secret", body)) == (200, {"status": "duplicate"})
    assert count_outbox(db) == 1


def test_webhook_http_route_and_dvir(db):
    rx = WebhookReceiver(db, AG)
    c = client(rx.app())
    body = json.dumps({"eventId": "d1", "vehicle": {"id": "T-77"}, "dvir": {"defect_text": "brakes", "reported_by_role": "operator"},
                       "time": "2026-01-01T00:00:00Z"}).encode()
    assert c.post("/webhook", content=body, headers={"X-Signature": "sha256=x"}).status_code == 401
    assert c.post("/webhook", content=body, headers={"X-Signature": sign("dev-secret", body)}).status_code == 200
    assert json.loads(db.execute("SELECT payload FROM outbox").fetchone()["payload"])["type"] == "DefectReported"


def test_backfill_reconciled_with_webhook(db):
    rx = WebhookReceiver(db, AG)
    p1 = fakes.fault_payload("evt-1")
    body = json.dumps(p1).encode()
    rx.handle(body, sign("dev-secret", body))
    state = {"feed": [p1, fakes.fault_payload("evt-2", spn=200, time="2026-01-01T11:00:00Z")]}
    bf = TelematicsBackfillPoller(db, client(fakes.telematics_app(state)), AG)
    assert bf.poll() == 1  # evt-1 already seen via webhook
    assert bf.poll() == 0
    assert count_outbox(db) == 2


# ---- relay ----------------------------------------------------------------------------------
def test_relay_crash_after_send_republishes_and_consumers_dedupe(db, bus, relay, svc, xw):
    xw.resolve(CrosswalkEntry(agency=AG, product="telematics", native_id="T-77", entity="vehicle", canonical_id="veh-1"))
    rx = WebhookReceiver(db, AG)
    body = json.dumps(fakes.fault_payload()).encode()
    rx.handle(body, sign("dev-secret", body))

    def crash():
        raise RuntimeError("crash before mark sent")

    relay.after_flush_hook = crash
    with pytest.raises(RuntimeError):
        relay.run_once()
    relay.after_flush_hook = None
    assert len(bus.messages(TOPIC_RAW)) == 1
    relay.run_once()  # republish
    assert len(bus.messages(TOPIC_RAW)) == 2
    svc.drain()
    assert len(bus.messages(TOPIC_RESOLVED)) == 1  # consumer deduped on event id
    assert relay.run_once() == 0


# ---- crosswalk service ----------------------------------------------------------------------
def add_map(xw, product, native, canon, entity="vehicle"):
    xw.resolve(CrosswalkEntry(agency=AG, product=product, native_id=native, entity=entity, canonical_id=canon))


def test_enrichment_and_replay_no_extra_resolved(db, bus, svc, xw):
    add_map(xw, "telematics", "T-77", "veh-1")
    add_map(xw, "maintenance", "1106", "veh-1")
    bus.send(TOPIC_RAW, "k", ev("e1"))
    svc.drain()
    (m,) = bus.messages(TOPIC_RESOLVED)
    assert m[1] == "veh-1"
    refs = m[2].data["refs"]
    assert refs["canonical"] == "veh-1" and refs["telematics"] == "T-77" and refs["maintenance"] == "1106"
    bus.reset_group(TOPIC_RAW, "crosswalk", 0)  # replay events.raw
    svc.drain()
    assert len(bus.messages(TOPIC_RESOLVED)) == 1


def test_per_vehicle_ordering_across_two_products(db, bus, svc, xw):
    add_map(xw, "telematics", "T-77", "veh-1")
    add_map(xw, "maintenance", "1106", "veh-1")
    add_map(xw, "telematics", "T-78", "veh-2")
    seq = []
    for i in range(6):
        if i % 3 == 0:
            e = ev(f"f{i}")
        elif i % 3 == 1:
            e = ev(f"w{i}", "WorkOrderOpened", native_asset_id="1106", wo_no=f"W{i}")
            e.data.pop("native_vehicle_id")
        else:
            e = ev(f"o{i}", native_vehicle_id="T-78")
        seq.append(e.id)
        bus.send(TOPIC_RAW, "whatever", e)
    svc.drain()
    out = bus.messages(TOPIC_RESOLVED)
    v1 = [m[2].id for m in out if m[1] == "veh-1"]
    assert v1 == [i for i in seq if i[0] in "fw"]
    assert [m[2].id for m in out if m[1] == "veh-2"] == [i for i in seq if i[0] == "o"]


def test_unknown_vehicle_parked_then_fixed_republished(db, bus, svc, xw):
    bus.send(TOPIC_RAW, "k", ev("e1", native_vehicle_id="T-99"))
    svc.drain()
    assert not bus.messages(TOPIC_RESOLVED)
    assert [m[2].id for m in bus.messages(TOPIC_FAILED)] == ["e1"]
    assert len(xw.unresolved()) == 1
    bus.reset_group(TOPIC_RAW, "crosswalk", 0)
    svc.drain()
    assert len(bus.messages(TOPIC_FAILED)) == 1  # replay does not re-report
    n = svc.apply_mapping(CrosswalkEntry(agency=AG, product="telematics", native_id="T-99", entity="vehicle", canonical_id="veh-9"))
    assert n == 1 and not xw.unresolved()
    svc.drain()
    (m,) = bus.messages(TOPIC_RESOLVED)
    assert m[1] == "veh-9" and m[2].id == "e1"


def test_resolve_returns_parked_events_contract_api(db, bus, xw):
    xw.add_unresolved(AG, "telematics", "T-5", "e5", ev("e5", native_vehicle_id="T-5"))
    out = xw.resolve(CrosswalkEntry(agency=AG, product="telematics", native_id="T-5", entity="vehicle", canonical_id="veh-5"))
    assert [e.id for e in out] == ["e5"] and xw.lookup(AG, "telematics", "T-5") == "veh-5"
    # bare contract signature: body recovered from events.raw
    bus.send(TOPIC_RAW, "k", ev("e6", native_vehicle_id="T-6"))
    xw.add_unresolved(AG, "telematics", "T-6", "e6")
    out = xw.resolve(CrosswalkEntry(agency=AG, product="telematics", native_id="T-6", entity="vehicle", canonical_id="veh-6"))
    assert [e.id for e in out] == ["e6"]


def test_operator_events_keyed_by_canonical_operator(db, bus, svc, xw):
    add_map(xw, "workforce", "OP-310", "op-3", "operator")
    e = CloudEvent(id="a1", source="adapter/workforce/ag1", type="OperatorAvailabilityChanged", time="2026-01-01T00:00:00Z",
                   agencyid=AG, data={"native_operator_id": "OP-310", "status": "unavailable"})
    bus.send(TOPIC_RAW, "k", e)
    svc.drain()
    (m,) = bus.messages(TOPIC_RESOLVED)
    assert m[1] == "op-3" and m[2].data["operator_canonical"] == "op-3"


# ---- audit ----------------------------------------------------------------------------------
def test_audit_consumers_and_decisions(db, bus, svc, xw):
    add_map(xw, "telematics", "T-77", "veh-1")
    bus.send(TOPIC_RAW, "k", ev("e1"))
    svc.drain()
    raw, res = AuditConsumer(db, bus, "raw"), AuditConsumer(db, bus, "resolved")
    assert raw.run_once() == 1 and res.run_once() == 1
    bus.reset_group(TOPIC_RAW, "audit-raw", 0)
    raw.run_once()
    assert db.execute("SELECT count(*) c FROM audit_raw").fetchone()["c"] == 1
    with pytest.raises(Exception, match="append-only"):
        db.execute("DELETE FROM audit_raw")
    ds = DecisionStore(db)
    ds.record_decision(DecisionRecord(run_id="r1", plan_version=1, trigger_event_id="e1"))
    ds.record_decision(DecisionRecord(run_id="r1", plan_version=2, trigger_event_id="e1", outcome="done"))
    ds.record_decision(DecisionRecord(run_id="r2", plan_version=1, trigger_event_id="e2"))
    assert [d.plan_version for d in ds.list_for_run("r1")] == [1, 2]
    with pytest.raises(Exception, match="append-only"):
        db.execute("UPDATE decisions SET outcome='x'")
