"""Kafka bus (Kafka profile): the same Producer/Consumer protocol as SqliteBus, on Apache Kafka.

Install the optional dependency first: `uv sync --extra kafka` (or `pip install 'transitos[kafka]'`).
This module always imports; constructing a KafkaBus without confluent-kafka raises a clear ImportError.

Producer
  Idempotent producer (enable.idempotence=true, acks=all): no duplicates or reordering from internal
  retries. The partition is chosen from the key with murmur2 (the Java client's partitioner), so every
  event of one key lands on one partition and keeps its order. flush() blocks until every queued message
  is acknowledged and raises if any delivery failed (the relay then leaves the outbox row unsent and
  republishes it later: at-least-once).
  Wire format: CloudEvents structured mode, value = the envelope as JSON, header
  content-type: application/cloudevents+json; Kafka key = the contract key (docs/CONTRACTS.md).

Consumer: how the contract's (offset, event) maps onto Kafka
  Kafka offsets are per partition, the contract has one int per (topic, group) and every caller commits
  only the LAST offset it saw ("commit after side effects are durable" covers everything before it).
  poll() therefore yields a delivery token: an int that grows with every message this reader yields.
  The reader remembers (token -> partition, offset); commit(token) commits, for every partition, the
  highest offset yielded at or before that token in the same delivery session (+1, Kafka's "next offset"
  convention), synchronously, to the group's offsets in Kafka (__consumer_offsets). A delivery session is
  the run of polls since the last rewind, so committing a token from an earlier (since redelivered) poll
  covers exactly that poll's prefix, as an SqliteBus offset would. Tokens are comparable within one bus
  instance; they are not stable across processes (Kafka's committed offsets are what survives).
  Same semantics as SqliteBus:
    * poll() never advances the group: a poll after an uncommitted poll starts again at the committed
      offsets (the reader re-assigns its partitions at the committed positions), so uncommitted messages
      are redelivered (at-least-once) exactly as in SqliteBus.
    * poll() returns everything that is in the log when it is called (it reads up to the partitions'
      high watermarks), so a deterministic driver (System.tick/run_until_idle) sees "idle" correctly.
    * ordering is per key (per partition), not global: messages of different keys can interleave.
    * reset_group(topic, group, 0) rewinds the group to the start of the log (replay).
  Group membership: each (topic, group) reader assigns itself ALL partitions of the topic and commits
  under the group id (Kafka's "manual assignment" consumer). Committed offsets, replay and isolation
  between groups are Kafka's; dynamic partition balancing between several processes of one group
  (subscribe + rebalance) is not used, because the POC driver is single-process and deterministic and a
  crashed member would otherwise hold its partitions until the session timeout. See docs/ARCHITECTURE.md.

Namespaces: `namespace="ns"` prefixes topic names and group ids (`ns.events.raw`, `ns.crosswalk`), so
several isolated systems (tests, scenarios) share one broker. namespace="" uses the canonical names.
"""
from __future__ import annotations

import heapq
import os
import socket
import time
from collections.abc import Iterator
from typing import Any

from transitos.contracts.envelope import CloudEvent

try:  # optional dependency (extra "kafka")
    import confluent_kafka as ck
    from confluent_kafka.admin import AdminClient, NewTopic, OffsetSpec
    _IMPORT_ERROR: ImportError | None = None
except ImportError as e:  # pragma: no cover - exercised by test_kafka_import_error_is_clear
    ck = None  # type: ignore[assignment]
    AdminClient = NewTopic = OffsetSpec = None  # type: ignore[assignment,misc]
    _IMPORT_ERROR = e

DEFAULT_BOOTSTRAP = "127.0.0.1:9092"
PARTITIONS = 3
MAX_SESSIONS = 8   # uncommitted delivery sessions a reader remembers (older tokens commit nothing)
REPLICATION = 1
RETENTION_MS = 7 * 24 * 3600 * 1000
CONTENT_TYPE = "application/cloudevents+json; charset=UTF-8"
INSTALL_HINT = ("KafkaBus needs the optional dependency confluent-kafka: run `uv sync --extra kafka` "
                "(or `pip install 'transitos[kafka]'`)")


class KafkaBusError(RuntimeError):
    """A Kafka operation did not complete (delivery failed, broker unreachable, read timed out)."""


def bootstrap_servers() -> str:
    return os.environ.get("TRANSITOS_KAFKA") or DEFAULT_BOOTSTRAP


def kafka_available() -> bool:
    return _IMPORT_ERROR is None


def broker_reachable(bootstrap: str | None = None, timeout: float = 1.0) -> bool:
    """TCP check of the first bootstrap server (cheap; used to auto-skip Kafka tests)."""
    host, _, port = (bootstrap or bootstrap_servers()).split(",")[0].rpartition(":")
    try:
        with socket.create_connection((host or "127.0.0.1", int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _require() -> None:
    if _IMPORT_ERROR is not None:
        raise ImportError(INSTALL_HINT) from _IMPORT_ERROR


def _decode(msg: Any) -> CloudEvent:
    return CloudEvent.model_validate_json(msg.value())


class _Reader:
    """One (topic, group) consumer: all partitions assigned, positions = committed offsets."""

    def __init__(self, bus: KafkaBus, topic: str, group: str):
        self.bus, self.topic, self.group = bus, topic, group
        self.c = ck.Consumer(bus._conf(**{
            "group.id": group, "enable.auto.commit": False, "enable.auto.offset.store": False,
            "auto.offset.reset": "earliest", "fetch.wait.max.ms": 50}))
        self.parts = bus._partitions(topic)
        tps = self.c.committed([ck.TopicPartition(topic, p) for p in self.parts], timeout=bus.timeout)
        low = bus._offsets(topic, self.parts, earliest=True)
        self.committed: dict[int, int] = {}
        for tp in tps:
            if tp.error:
                raise KafkaBusError(f"committed offsets for {group}/{topic}: {tp.error}")
            self.committed[tp.partition] = tp.offset if tp.offset >= 0 else low[tp.partition]
        self.fetched: dict[int, int] = {}   # next offset per partition after what consume() returned
        self.token = 0
        # delivery session = yields since the last re-assign; within one session tokens follow yield order
        self.session = 0
        self.pending: list[tuple[int, int, int, int]] = []   # (token, session, partition, offset), uncommitted
        self._assign()

    def _assign(self) -> None:
        self.c.assign([ck.TopicPartition(self.topic, p, self.committed[p]) for p in self.parts])
        self.fetched = dict(self.committed)

    def poll(self, limit: int) -> Iterator[tuple[int, CloudEvent]]:
        if self.fetched != self.committed:   # uncommitted deliveries: start again at the committed offsets
            self._assign()
            self.session += 1   # earlier sessions' tokens stay committable (a caller may commit an older
            self.pending = [e for e in self.pending if e[1] > self.session - MAX_SESSIONS]   # delivery)
        high = self.bus._offsets(self.topic, self.parts)
        want = min(limit, sum(max(0, high[p] - self.fetched[p]) for p in self.parts))
        got, deadline = 0, time.monotonic() + self.bus.timeout
        while got < want:
            msgs = self.c.consume(num_messages=want - got, timeout=min(1.0, max(0.05, deadline - time.monotonic())))
            if not msgs:
                pos = {tp.partition: tp.offset for tp in self.c.position(
                    [ck.TopicPartition(self.topic, p) for p in self.parts])}
                if all(pos.get(p, -1) >= high[p] for p in self.parts):
                    return   # offsets had gaps (e.g. control records): nothing more to read
                if time.monotonic() > deadline:
                    raise KafkaBusError(f"timed out reading {self.topic} for {self.group}: positions {pos}, "
                                        f"high watermarks {high}")
                continue
            for m in msgs:
                if m.error():
                    raise KafkaBusError(f"consume {self.topic}: {m.error()}")
                p, off = m.partition(), m.offset()
                if off < self.fetched[p]:   # stale prefetched message from before a re-assign
                    continue
                self.fetched[p] = off + 1
                got += 1
                self.token += 1
                self.pending.append((self.token, self.session, p, off))
                yield self.token, _decode(m)

    def commit(self, token: int) -> None:
        """Commit what was yielded up to `token` IN THE DELIVERY SESSION that yielded it (so committing a
        token from an earlier, redelivered poll covers exactly that poll's prefix, as in SqliteBus)."""
        sess = next((e[1] for e in self.pending if e[0] == token), None)
        if sess is None:
            return   # unknown or already committed token
        upto: dict[int, int] = {}
        for t, s, p, off in self.pending:
            if s == sess and t <= token:
                upto[p] = max(upto.get(p, -1), off + 1)
        self.pending = [e for e in self.pending if e[1] > sess or (e[1] == sess and e[0] > token)]
        new = {p: o for p, o in upto.items() if o > self.committed[p]}   # monotonic, like SqliteBus
        if not new:
            return
        self.c.commit(offsets=[ck.TopicPartition(self.topic, p, o) for p, o in new.items()], asynchronous=False)
        self.committed.update(new)

    def reset(self) -> None:
        start = self.bus._offsets(self.topic, self.parts, earliest=True)
        self.c.commit(offsets=[ck.TopicPartition(self.topic, p, o) for p, o in start.items()], asynchronous=False)
        self.committed, self.pending = start, []
        self._assign()

    def close(self) -> None:
        self.c.close()   # no auto-commit: closing never commits anything


class KafkaBus:
    """Implements contracts.bus.Producer and Consumer on Apache Kafka (see module docstring)."""

    def __init__(self, bootstrap: str | None = None, *, namespace: str = "", partitions: int = PARTITIONS,
                 replication: int = REPLICATION, retention_ms: int = RETENTION_MS, timeout: float = 15.0,
                 client_id: str = "transitos"):
        _require()
        self.bootstrap = bootstrap or bootstrap_servers()
        self.namespace, self.partitions, self.replication = namespace, partitions, replication
        self.retention_ms, self.timeout, self.client_id = retention_ms, timeout, client_id
        self._admin = AdminClient(self._conf())
        self._producer = ck.Producer(self._conf(**{
            "enable.idempotence": True, "acks": "all", "partitioner": "murmur2_random",
            "linger.ms": 1, "message.timeout.ms": 60000}))
        self._errors: list[Any] = []
        self._readers: dict[tuple[str, str], _Reader] = {}
        self._topics: dict[str, list[int]] = {}
        self._closed = False

    # ------------------------------------------------------------------ names / config
    def topic_name(self, topic: str) -> str:
        return f"{self.namespace}.{topic}" if self.namespace else topic

    def group_id(self, group: str) -> str:
        return f"{self.namespace}.{group}" if self.namespace else group

    def _conf(self, **extra: Any) -> dict[str, Any]:
        return {"bootstrap.servers": self.bootstrap, "client.id": self.client_id,
                "reconnect.backoff.max.ms": 1000, "socket.connection.setup.timeout.ms": 5000, **extra}

    def _partitions(self, name: str) -> list[int]:
        """Ensure the (namespaced) topic exists; return its partition ids."""
        if name in self._topics:
            return self._topics[name]
        deadline = time.monotonic() + self.timeout
        created = False
        while True:
            try:
                md = self._admin.list_topics(topic=name, timeout=self.timeout).topics.get(name)
            except ck.KafkaException as e:
                raise KafkaBusError(f"broker {self.bootstrap} unreachable: {e}") from e
            if md is not None and md.error is None and md.partitions and \
                    all(pm.leader >= 0 for pm in md.partitions.values()):
                self._topics[name] = sorted(md.partitions)
                return self._topics[name]
            if not created:
                fut = self._admin.create_topics([NewTopic(
                    name, num_partitions=self.partitions, replication_factor=self.replication,
                    config={"retention.ms": str(self.retention_ms)})])[name]
                try:
                    fut.result(timeout=self.timeout)
                except ck.KafkaException as e:
                    if e.args[0].code() != ck.KafkaError.TOPIC_ALREADY_EXISTS:
                        raise KafkaBusError(f"create topic {name}: {e}") from e
                created = True
            if time.monotonic() > deadline:
                raise KafkaBusError(f"topic {name} has no leader after {self.timeout}s")
            time.sleep(0.05)

    def _offsets(self, name: str, parts: list[int], earliest: bool = False) -> dict[int, int]:
        """Low or high watermark per partition, in one ListOffsets request on the admin connection (a
        consumer's own connection would queue the request behind its outstanding long-poll fetch)."""
        spec = OffsetSpec.earliest() if earliest else OffsetSpec.latest()
        futs = self._admin.list_offsets({ck.TopicPartition(name, p): spec for p in parts},
                                        request_timeout=self.timeout)
        try:
            return {tp.partition: f.result(timeout=self.timeout).offset for tp, f in futs.items()}
        except ck.KafkaException as e:
            raise KafkaBusError(f"list offsets {name}: {e}") from e

    def _reader(self, topic: str, group: str) -> _Reader:
        k = (topic, group)
        if k not in self._readers:
            name = self.topic_name(topic)
            self._partitions(name)
            self._readers[k] = _Reader(self, name, self.group_id(group))
        return self._readers[k]

    # ------------------------------------------------------------------ Producer
    def _on_delivery(self, err: Any, _msg: Any) -> None:
        if err is not None:
            self._errors.append(err)

    def send(self, topic: str, key: str, event: CloudEvent) -> None:
        name = self.topic_name(topic)
        self._partitions(name)
        value = event.model_dump_json().encode()
        while True:
            try:
                self._producer.produce(name, key=key.encode(), value=value,
                                       headers=[("content-type", CONTENT_TYPE.encode())],
                                       on_delivery=self._on_delivery)
                break
            except BufferError:   # local queue full: serve delivery reports, then retry
                self._producer.poll(0.5)
        self._producer.poll(0)

    def flush(self) -> None:
        """Wait until every sent message is acknowledged by the broker (acks=all); raise otherwise."""
        remaining = self._producer.flush(self.timeout)
        if remaining:
            raise KafkaBusError(f"{remaining} message(s) not acknowledged within {self.timeout}s")
        if self._errors:
            errs, self._errors = self._errors, []
            raise KafkaBusError(f"delivery failed: {errs[0]} ({len(errs)} message(s))")

    # ------------------------------------------------------------------ Consumer
    def poll(self, topic: str, group: str, limit: int = 100) -> Iterator[tuple[int, CloudEvent]]:
        """Yield (token, event) after the group's committed offsets, in order per key (see module doc)."""
        if len(self._producer):   # read-your-writes for this bus instance
            self.flush()
        yield from self._reader(topic, group).poll(limit)

    def commit(self, topic: str, group: str, offset: int) -> None:
        """Commit everything this reader yielded up to token `offset`. Call only after side effects are durable."""
        self._reader(topic, group).commit(offset)

    # ------------------------------------------------------------------ admin / replay (same names as SqliteBus)
    def reset_group(self, topic: str, group: str, offset: int = 0) -> None:
        """Replay: rewind the group to the start of the log. Only offset 0 (= earliest) is meaningful on
        Kafka; stop other readers of the group first (as with kafka-consumer-groups --reset-offsets)."""
        if offset != 0:
            raise ValueError("KafkaBus.reset_group supports offset 0 (earliest) only")
        self._reader(topic, group).reset()

    def committed(self, topic: str, group: str) -> dict[int, int]:
        return dict(self._reader(topic, group).committed)

    def _read_all(self, topic: str) -> list[tuple[int, str, CloudEvent]]:
        name = self.topic_name(topic)
        parts = self._partitions(name)
        c = ck.Consumer(self._conf(**{"group.id": f"{self.client_id}-reader-{os.getpid()}-{time.monotonic_ns()}",
                                      "enable.auto.commit": False, "fetch.wait.max.ms": 50}))
        try:
            lo, hi = self._offsets(name, parts, earliest=True), self._offsets(name, parts)
            marks = {p: (lo[p], hi[p]) for p in parts}
            want = {p: hi for p, (lo, hi) in marks.items() if hi > lo}
            if not want:
                return []
            c.assign([ck.TopicPartition(name, p, marks[p][0]) for p in want])
            per: dict[int, list[tuple[int, int, int, str, CloudEvent]]] = {p: [] for p in want}
            nxt = {p: marks[p][0] for p in want}
            deadline = time.monotonic() + self.timeout
            while any(nxt[p] < want[p] for p in want):
                if time.monotonic() > deadline:
                    raise KafkaBusError(f"timed out reading {name}")
                left = sum(max(0, want[p] - nxt[p]) for p in want)
                for m in c.consume(num_messages=min(left, 1000), timeout=0.5):
                    if m.error():
                        raise KafkaBusError(f"consume {name}: {m.error()}")
                    p, off = m.partition(), m.offset()
                    if off >= want[p]:
                        continue
                    nxt[p] = off + 1
                    per[p].append((m.timestamp()[1], p, off, m.key().decode(), _decode(m)))
                if any(nxt[p] < want[p] for p in want):
                    pos = {tp.partition: tp.offset for tp in c.position([ck.TopicPartition(name, p) for p in want])}
                    for p in want:
                        nxt[p] = max(nxt[p], pos.get(p, -1))
        finally:
            c.close()
        # merge partitions by produce timestamp; heapq.merge keeps each partition's (per-key) order
        merged = heapq.merge(*per.values(), key=lambda r: r[0])
        return [((p << 40) | off, key, ev) for _, p, off, key, ev in merged]

    def messages(self, topic: str) -> list[tuple[int, str, CloudEvent]]:
        """(position, key, event) for every message in the topic. Order: per partition (so per key) exact,
        across partitions by produce timestamp. position = (partition << 40) | offset."""
        if len(self._producer):
            self.flush()
        return self._read_all(topic)

    def find(self, topic: str, event_id: str) -> CloudEvent | None:
        return next((ev for _, _, ev in self.messages(topic) if ev.id == event_id), None)

    def delete_topics(self, topics: list[str] | None = None) -> None:
        """Delete this namespace's topics (tests). Refuses on the canonical (empty) namespace."""
        if not self.namespace:
            raise ValueError("refusing to delete canonical topics")
        names = [self.topic_name(t) for t in topics] if topics else list(self._topics)
        if names:
            for f in self._admin.delete_topics(names, operation_timeout=self.timeout).values():
                try:
                    f.result(timeout=self.timeout)
                except ck.KafkaException:
                    pass
        self._topics.clear()

    def close(self) -> None:
        """Release clients. Does not commit anything (uncommitted deliveries are redelivered next time)."""
        if self._closed:
            return
        self._closed = True
        for r in self._readers.values():
            try:
                r.close()
            except Exception:  # noqa: BLE001,S110 - best effort on shutdown
                pass
        self._readers.clear()
        try:
            self._producer.flush(min(self.timeout, 5.0))
        except Exception:  # noqa: BLE001,S110
            pass
