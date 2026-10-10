"""Kafka profile: the whole system on a real broker (KafkaBus), crash/redelivery, replay, ordering with
3 partitions, broker restart. Marked `kafka` (auto-skipped without a broker); the broker-restart tests are
`kafka_admin` (they stop/start the project-local broker via scripts/kafka.sh; TRANSITOS_KAFKA_ADMIN=1)."""
from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest

from transitos.contracts.crosswalk import CrosswalkEntry
from transitos.contracts.envelope import TOPIC_RAW, TOPIC_RESOLVED, CloudEvent
from transitos.integration.world import IntegrationWorld
from transitos.plumbing import CrosswalkService, Db, KafkaBus, Relay, SqliteCrosswalk
from transitos.plumbing.kafka_bus import KafkaBusError
from transitos.verification.invariants import check_all
from transitos.verification.scenarios import AGENCY, BY_ID

pytestmark = pytest.mark.kafka

ROOT = Path(__file__).resolve().parents[2]
H = {"X-Agency": AGENCY}


class Crash(BaseException):
    """Process killed (not an Exception: nothing in the system may swallow it)."""


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setenv("TRANSITOS_BUS", "kafka")
    w = IntegrationWorld()
    w.apply(BY_ID["operator_swap_needed"])
    assert isinstance(w.system.bus, KafkaBus)
    yield w
    w.system.close()
    w.system.bus.delete_topics()


def fault_via_mock(w: IntegrationWorld) -> None:
    r = w.mocks.clients["telematics"].post("/sim/fault", headers=H,
                                           json={"vehicle": "T-77", "spn": 520, "fmi": 3, "lamps": {"stop": True}})
    r.raise_for_status()
    w.system.run_until_idle()


def resolved_faults(s) -> list[CloudEvent]:
    return [e for _, _, e in s.bus.messages(TOPIC_RESOLVED) if e.type == "VehicleFaultDetected"]


def crash_on_commit(s, topic: str, group: str) -> None:
    """The next commit of (topic, group) kills the process: side effects are durable, the offset is not."""
    real = s.bus.commit

    def commit(t, g, offset):
        if (t, g) == (topic, group):
            raise Crash(f"killed before committing {g}")
        real(t, g, offset)
    s.bus.commit = commit


# ---------------------------------------------------------------- whole system on Kafka
def test_whole_system_happy_path_on_kafka(world):
    s = world.system
    fault_via_mock(world)
    (run,) = s.store.list_runs()
    rid = run["run_id"]
    (res,) = resolved_faults(s)
    assert res.canonicalid == "veh-1" and res.data["refs"]["scheduling"] == "BUS-44"
    world.approve(rid)
    world.advance_clock(48)
    world.close_work_order(rid)
    world.approve(rid, scope="return_to_service")
    assert s.gateway.get_run(rid).status == "DONE"
    assert [w.action for w in s.writes.records] == ["create_work_order", "set_vehicle_unavailable", "reassign_block",
                                                   "assign_operator", "publish_rider_alert", "set_vehicle_available"]
    assert check_all(world.observe()) == []
    ns = s.bus.namespace   # the topics really are on the broker, 3 partitions each
    md = s.bus._admin.list_topics(timeout=10).topics
    assert {f"{ns}.{t}" for t in ("events.raw", "events.resolved")} <= set(md)
    assert len(md[f"{ns}.events.resolved"].partitions) == 3


def test_workflow_consumer_killed_before_commit_redelivers_and_starter_dedupes(world):
    s = world.system
    crash_on_commit(s, TOPIC_RESOLVED, "workflow")
    with pytest.raises(Crash):
        fault_via_mock(world)
    (run,) = s.store.list_runs()                       # the run was started (checkpoint durable) ...
    seen: list[tuple[str, str]] = []
    s.on_resolved = lambda ev, res: seen.append((ev.id, res.action))
    world.restart()                                     # ... the offset was not: Kafka redelivers
    fault_id = resolved_faults(s)[0].id
    assert (fault_id, "duplicate") in seen
    assert [r["run_id"] for r in s.store.list_runs()] == [run["run_id"]]
    assert s.gateway.get_run(run["run_id"]).status == "AWAITING_APPROVAL" and s.writes.records == []


def test_crosswalk_killed_before_commit_redelivers_without_second_resolved_event(world):
    s = world.system
    crash_on_commit(s, TOPIC_RAW, "crosswalk")
    with pytest.raises(Crash):
        fault_via_mock(world)
    world.restart()
    raw = [e for _, _, e in s.bus.messages(TOPIC_RAW) if e.type == "VehicleFaultDetected"]
    assert len(raw) == 1 and len(resolved_faults(s)) == 1   # redelivered raw deduped by event id
    assert len(s.store.list_runs()) == 1


def test_replay_of_raw_and_resolved_creates_no_extra_events_runs_or_writes(world):
    s = world.system
    fault_via_mock(world)
    (run,) = s.store.list_runs()
    world.approve(run["run_id"])
    n_res, n_writes = len(s.bus.messages(TOPIC_RESOLVED)), len(s.writes.records)
    s.bus.reset_group(TOPIC_RAW, "crosswalk", 0)
    s.bus.reset_group(TOPIC_RESOLVED, "workflow", 0)
    s.run_until_idle()
    assert len(s.bus.messages(TOPIC_RESOLVED)) == n_res
    assert len(s.store.list_runs()) == 1 and len(s.writes.records) == n_writes


# ---------------------------------------------------------------- plumbing on Kafka
@pytest.fixture
def plumbing(tmp_path):
    db = Db(str(tmp_path / "p.db"))
    bus = KafkaBus(namespace=f"test-{uuid.uuid4().hex[:10]}")
    xw = SqliteCrosswalk(db, bus)
    yield db, bus, xw, CrosswalkService(db, bus, xw), Relay(db, bus)
    bus.close()
    bus.delete_topics()


def _ev(i: str, type_: str, **data) -> CloudEvent:
    product = "telematics" if type_ == "VehicleFaultDetected" else "maintenance"
    return CloudEvent(id=i, source=f"adapter/{product}/ag1", type=type_, time="2026-01-01T00:00:00Z",
                      agencyid="ag1", data=data)


def test_per_vehicle_order_on_resolved_with_3_partitions_across_two_products(plumbing):
    """Real keys: events.raw is keyed <product>:<native id>, events.resolved by canonical vehicle id."""
    _db, bus, xw, svc, _ = plumbing
    n = 12
    for v in range(n):
        for product, native in (("telematics", f"T-{v}"), ("maintenance", f"M-{v}")):
            xw.resolve(CrosswalkEntry(agency="ag1", product=product, native_id=native, entity="vehicle",
                                      canonical_id=f"veh-{v}"))
    sent: dict[tuple[int, str], list[str]] = {}
    for i in range(4):
        for v in range(n):
            f = _ev(f"f{v}-{i}", "VehicleFaultDetected", native_vehicle_id=f"T-{v}", spn=i, fmi=1)
            w = _ev(f"w{v}-{i}", "WorkOrderOpened", native_asset_id=f"M-{v}", wo_no=f"W{v}{i}")
            bus.send(TOPIC_RAW, f"telematics:T-{v}", f)
            bus.send(TOPIC_RAW, f"maintenance:M-{v}", w)
            sent.setdefault((v, "f"), []).append(f.id)
            sent.setdefault((v, "w"), []).append(w.id)
    bus.flush()
    assert svc.drain() == 8 * n
    out = bus.messages(TOPIC_RESOLVED)
    assert len(out) == 8 * n and len(bus._topics[bus.topic_name(TOPIC_RESOLVED)]) == 3
    parts = {(pos >> 40) for pos, _, _ in out}
    assert len(parts) >= 2                                   # canonical keys really spread over partitions
    for v in range(n):
        mine = [(pos, e) for pos, k, e in out if k == f"veh-{v}"]
        assert len({pos >> 40 for pos, _ in mine}) == 1      # one vehicle -> one partition
        ids = [e.id for _, e in mine]
        for kind in ("f", "w"):                              # each product's order per vehicle is kept
            assert [x for x in ids if x[0] == kind] == sent[(v, kind)]
    # every consumer group of events.resolved sees the same per-vehicle order
    for group in ("g1", "g2"):
        got: dict[str, list[str]] = {}
        last = None
        for off, e in bus.poll(TOPIC_RESOLVED, group, limit=1000):
            got.setdefault(e.canonicalid, []).append(e.id)
            last = off
        bus.commit(TOPIC_RESOLVED, group, last)
        for v in range(n):
            assert got[f"veh-{v}"] == [e.id for _, k, e in out if k == f"veh-{v}"]


def test_relay_crash_after_send_republishes_and_crosswalk_dedupes(plumbing):
    from transitos.plumbing import outbox
    db, bus, xw, svc, relay = plumbing
    xw.resolve(CrosswalkEntry(agency="ag1", product="telematics", native_id="T-1", entity="vehicle",
                              canonical_id="veh-1"))
    with db.tx() as c:
        outbox.add(c, _ev("e1", "VehicleFaultDetected", native_vehicle_id="T-1", spn=1, fmi=1), TOPIC_RAW,
                   "telematics:T-1")

    def crash():
        raise Crash("after flush, before marking sent")
    relay.after_flush_hook = crash
    with pytest.raises(Crash):
        relay.run_once()
    relay.after_flush_hook = None
    assert relay.run_once() == 1                             # republished: acked twice on Kafka
    assert [e.id for _, _, e in bus.messages(TOPIC_RAW)] == ["e1", "e1"]
    svc.drain()
    assert [e.id for _, _, e in bus.messages(TOPIC_RESOLVED)] == ["e1"]


def test_unreachable_broker_fails_loudly_and_relay_keeps_the_row(tmp_path):
    from transitos.plumbing import outbox
    db = Db(str(tmp_path / "x.db"))
    bus = KafkaBus("127.0.0.1:1", namespace="nobroker", timeout=2.0)
    relay = Relay(db, bus)
    with db.tx() as c:
        outbox.add(c, _ev("e1", "VehicleFaultDetected", native_vehicle_id="T-1"), TOPIC_RAW, "telematics:T-1")
    with pytest.raises(KafkaBusError):
        relay.run_once()
    assert db.execute("SELECT count(*) c FROM outbox WHERE sent_at IS NULL").fetchone()["c"] == 1
    bus.close()


# ---------------------------------------------------------------- broker restart (stops the local broker)
def kafka_sh(cmd: str) -> None:
    subprocess.run([str(ROOT / "scripts" / "kafka.sh"), cmd], check=True, capture_output=True, timeout=180)


@pytest.mark.kafka_admin
def test_broker_restart_keeps_committed_offsets_and_unconsumed_messages():
    ns = f"test-{uuid.uuid4().hex[:10]}"
    bus = KafkaBus(namespace=ns)
    ev = [_ev(f"e{i}", "VehicleFaultDetected", native_vehicle_id="T-1") for i in range(7)]
    for i, e in enumerate(ev[:5]):
        bus.send("t", f"k{i % 3}", e)
    bus.flush()
    batch = list(bus.poll("t", "g", limit=3))
    bus.commit("t", "g", batch[-1][0])
    done = {e.id for _, e in batch}
    for i, e in enumerate(ev[5:]):
        bus.send("t", f"k{i % 3}", e)
    bus.flush()
    before = bus.committed("t", "g")
    bus.close()
    kafka_sh("down")
    kafka_sh("up")
    bus2 = KafkaBus(namespace=ns)
    try:
        assert bus2.committed("t", "g") == before
        rest = [e.id for _, e in bus2.poll("t", "g")]
        assert sorted(rest) == sorted({e.id for e in ev} - done)   # nothing lost, nothing committed redelivered
    finally:
        bus2.close()
        bus2.delete_topics(["t"])


@pytest.mark.kafka_admin
def test_broker_restart_mid_run_loses_no_committed_work(world):
    s = world.system
    fault_via_mock(world)
    (run,) = s.store.list_runs()
    committed = s.bus.committed(TOPIC_RESOLVED, "workflow")
    s.bus.close()
    kafka_sh("down")
    kafka_sh("up")
    world.restart()                                          # new clients against the restarted broker
    assert s.bus.committed(TOPIC_RESOLVED, "workflow") == committed
    world.approve(run["run_id"])
    assert s.gateway.get_run(run["run_id"]).status == "WAITING_FOR_REPAIR"
    assert len(s.store.list_runs()) == 1 and len(resolved_faults(s)) == 1
    assert [w.action for w in s.writes.records] == ["create_work_order", "set_vehicle_unavailable", "reassign_block",
                                                   "assign_operator", "publish_rider_alert"]
