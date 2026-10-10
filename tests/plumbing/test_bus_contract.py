"""Shared contract suite for contracts.bus.Producer/Consumer, run on SqliteBus and KafkaBus (the `bus`
fixture in conftest.py; Kafka params are marked `kafka` and auto-skipped without a broker).

Semantics every implementation must give (contracts/bus.py): ordered per key, at-least-once,
consumer-owned offsets (commit only after side effects), replay. Offsets are opaque: callers only compare
offsets from the same poll session and commit the last one they processed.
"""
from __future__ import annotations

from transitos.contracts.envelope import CloudEvent

AG = "ag1"


def ev(i: str, **extra) -> CloudEvent:
    return CloudEvent(id=i, source=f"adapter/telematics/{AG}", type="VehicleFaultDetected",
                      time="2026-01-01T00:00:00Z", agencyid=AG, data={"native_vehicle_id": "T-1"}, **extra)


def send_all(bus, topic, pairs):
    for key, e in pairs:
        bus.send(topic, key, e)
    bus.flush()


def drain(bus, topic, group, limit=100):
    """Consume like the real consumers: process everything polled, then commit the last offset."""
    got, last = [], None
    while True:
        batch = list(bus.poll(topic, group, limit))
        if not batch:
            return got
        for off, e in batch:
            got.append(e)
            last = off
        bus.commit(topic, group, last)


def test_ordered_per_key_with_interleaved_keys(bus):
    keys = [f"veh-{k}" for k in range(5)]
    pairs = [(keys[i % 5], ev(f"{keys[i % 5]}:{i}")) for i in range(40)]
    send_all(bus, "c.order", pairs)
    got = drain(bus, "c.order", "g")
    assert sorted(e.id for e in got) == sorted(e.id for _, e in pairs)
    for k in keys:
        assert [e.id for e in got if e.id.startswith(k + ":")] == [e.id for kk, e in pairs if kk == k]


def test_at_least_once_redelivery_until_committed(bus):
    send_all(bus, "c.alo", [("k", ev(f"e{i}")) for i in range(3)])
    first = list(bus.poll("c.alo", "g"))
    assert [e.id for _, e in first] == ["e0", "e1", "e2"]
    assert [e.id for _, e in bus.poll("c.alo", "g")] == ["e0", "e1", "e2"]   # nothing committed: again
    again = list(bus.poll("c.alo", "g"))
    bus.commit("c.alo", "g", again[0][0])
    assert [e.id for _, e in bus.poll("c.alo", "g")] == ["e1", "e2"]
    rest = list(bus.poll("c.alo", "g"))
    bus.commit("c.alo", "g", rest[-1][0])
    assert list(bus.poll("c.alo", "g")) == []


def test_commit_is_isolated_per_group_and_every_group_gets_every_event(bus):
    send_all(bus, "c.groups", [(f"k{i % 3}", ev(f"e{i}")) for i in range(6)])
    a = drain(bus, "c.groups", "audit")
    assert len(a) == 6
    assert list(bus.poll("c.groups", "audit")) == []
    b = drain(bus, "c.groups", "workflow")                  # a's commit does not move b
    assert sorted(e.id for e in b) == sorted(e.id for e in a)
    send_all(bus, "c.groups", [("k9", ev("late"))])
    assert [e.id for e in drain(bus, "c.groups", "audit")] == ["late"]
    assert [e.id for e in drain(bus, "c.groups", "workflow")] == ["late"]


def test_replay_from_start(bus):
    send_all(bus, "c.replay", [(f"k{i % 2}", ev(f"e{i}")) for i in range(4)])
    assert len(drain(bus, "c.replay", "g")) == 4
    assert drain(bus, "c.replay", "g") == []
    bus.reset_group("c.replay", "g", 0)
    assert sorted(e.id for e in drain(bus, "c.replay", "g")) == ["e0", "e1", "e2", "e3"]
    assert len(drain(bus, "c.replay", "other")) == 4        # replay of g did not touch other groups


def test_limit_is_respected_and_commit_is_monotonic(bus):
    send_all(bus, "c.limit", [("k", ev(f"e{i}")) for i in range(5)])
    batch = list(bus.poll("c.limit", "g", limit=2))
    assert [e.id for _, e in batch] == ["e0", "e1"]
    bus.commit("c.limit", "g", batch[-1][0])
    bus.commit("c.limit", "g", batch[0][0])                  # an older commit never moves the group back
    assert [e.id for _, e in bus.poll("c.limit", "g")] == ["e2", "e3", "e4"]


def test_unknown_and_extra_fields_are_tolerated(bus):
    e = ev("x1", futureattr="v2-only", canonicalid="veh-1")
    e.data["new_field"] = {"nested": [1, 2]}
    send_all(bus, "c.extra", [("veh-1", e)])
    ((_, got),) = list(bus.poll("c.extra", "g"))
    assert got.id == "x1" and got.canonicalid == "veh-1"
    assert got.model_extra.get("futureattr") == "v2-only"
    assert got.data["new_field"] == {"nested": [1, 2]}


def test_offsets_compare_in_delivery_order(bus):
    send_all(bus, "c.offs", [(f"k{i % 3}", ev(f"e{i}")) for i in range(6)])
    offs = [o for o, _ in bus.poll("c.offs", "g")]
    assert offs == sorted(offs) and len(set(offs)) == 6
