# Handoff: real Kafka segment

## Built
- `scripts/kafka.sh fetch|up|down|status|reset|topics|purge` (+ `make kafka-up kafka-down kafka-status
  kafka-topics kafka-purge kafka-test verify-kafka`). Downloads the Apache Kafka 4.3.1 binary tarball
  (`kafka_2.13-4.3.1.tgz`, latest 4.x on downloads.apache.org on 2026-10-10) into `.local/kafka/`, verifies
  its SHA-512 against the published `.sha512` before extracting (verified digest kept in
  `.local/kafka/kafka_2.13-4.3.1.tgz.sha512.verified`), formats a single-node KRaft broker+controller with
  data in `.local/kafka-data`, listeners on 127.0.0.1:9092 (broker) / 127.0.0.1:9093 (controller) only, heap
  `-Xmx512m -Xms256m`, remote JMX off (the default opens an RMI port on all interfaces), auto topic creation off.
  `topics` creates `events.raw`, `events.resolved`, `events.failed`: 3 partitions, replication 1,
  retention.ms 604800000 (7 days). `purge` deletes test/scenario namespace topics in one AdminClient call.
- `src/transitos/plumbing/kafka_bus.py`: `KafkaBus` (replaces Track B's NotImplemented stub; same
  Producer/Consumer protocol plus SqliteBus's admin helpers `reset_group`, `messages`, `find`, `committed`,
  and `close`, `delete_topics`). Idempotent producer (`enable.idempotence=true`, `acks=all`, murmur2 key
  partitioning), `flush()` waits for every ack and raises on failure. Reader per (topic, group): manual
  commit only, uncommitted deliveries come back on the next poll, replay via `reset_group(.., 0)`, per-key
  order. CloudEvents structured JSON values with `content-type: application/cloudevents+json`. Retries
  errors the broker marks retriable (leader/coordinator loading after a restart) up to the timeout, then
  `KafkaBusError`. `src/transitos/plumbing/kafka_local.py`: purge helper used by the script.
- `confluent-kafka` is the optional extra `kafka` (`uv sync --extra kafka`). Without it `transitos.plumbing`
  still imports; `KafkaBus()` raises ImportError naming the install command.
- `System(bus=...)` / `TRANSITOS_BUS=sqlite|kafka` (default sqlite), `TRANSITOS_KAFKA` (bootstrap, default
  127.0.0.1:9092), `TRANSITOS_KAFKA_NAMESPACE` (default: one namespace per System data dir, persisted so
  `restart()` reconnects; `none` = canonical topic names). `System.close()`; `restart()` closes the Kafka
  clients without committing (crash semantics); `publish_raw` flushes; `IntegrationWorld.apply` closes the
  previous system.

## Offset mapping (no contract change)
The protocol's `poll() -> (offset, event)` / `commit(offset)` is kept unchanged. Every caller commits only
the last offset it processed, meaning "everything before it". On Kafka, `poll()` yields a per-reader
delivery token; `commit(token)` commits, per partition, the highest offset yielded at or before that token
in the same delivery session (a session = the polls since the last rewind), synchronously. This reproduces
SqliteBus exactly, including committing a token from an earlier, since-redelivered poll. Tokens are only
comparable inside one process; the durable state is Kafka's committed offsets (A53). poll() reads up to the
partitions' high watermarks (from one ListOffsets on the admin connection, ~0.5 ms idle poll), so
`run_until_idle()` sees idle correctly. No v1.2 contract change was needed.

## Tests
- `tests/plumbing/test_bus_contract.py` (new, 7 tests) and every bus-using test in `tests/plumbing` run on
  both buses through the parametrised `bus` fixture (Kafka params marked `kafka`).
- `tests/kafka/test_kafka_profile.py` (10 tests): whole system happy path on Kafka; workflow consumer
  killed before commit -> redelivered -> starter dedupes by event id; crosswalk killed before commit -> no
  second resolved event; relay crash after send republishes and the crosswalk dedupes; replay of raw and
  resolved creates no extra events, runs or writes; per-vehicle order on events.resolved with 3 partitions
  across two products (12 vehicles, real keys); unreachable broker -> KafkaBusError and the outbox row stays
  unsent; broker restart keeps committed offsets and unconsumed messages; broker restart mid-run (run
  awaiting approval) loses no committed work. The two broker-restart tests are `kafka_admin` (they run
  `scripts/kafka.sh down/up`; need TRANSITOS_KAFKA_ADMIN=1).
- Gating (`tests/conftest.py`): Kafka tests run when 127.0.0.1:9092 answers or TRANSITOS_KAFKA is set,
  else they are skipped; markers registered in pyproject.

## Results (run in this session)
- Broker: Kafka 4.3.1 on JDK 24 (`java version "24" 2025-03-18`) started and passed everything; no fallback
  JDK needed. SHA-512 verified: yes. Startup to ready 1.6 s (empty) / 4.7-6.3 s (after many test topics).
  Peak broker RSS 426 MiB (436192 kB) over `make verify-kafka`; idle RSS after start 182-404 MB.
- Default, no broker running: `make verify` -> `All checks passed!`, `320 passed, 24 skipped, 1 xfailed`
  (313 before + 7 SQLite contract tests; the stub test was replaced 1:1; the 24 skips are the Kafka params),
  `23/23 scenarios passed`.
- `make verify-kafka` (TRANSITOS_BUS=kafka: every System/World on Kafka; ruff + full pytest incl. broker
  restarts + Track F report): `All checks passed!`, `344 passed, 1 xfailed in 163.74s`, `23/23 scenarios
  passed`, `deleted 375 non-canonical topics`. Track F's real-World e2e tests all pass on Kafka; the one
  xfail is the pre-existing strict xfail A48 (same as SQLite).
- First failure found on the way and fixed: right after a broker restart, ListOffsets returned
  NOT_LEADER_FOR_PARTITION (KRaft still loading leadership); the bus now retries retriable errors until its
  timeout. Second: purging ~250 topics with one kafka-topics.sh JVM each took >6 min; replaced by one
  AdminClient call (0.7 s).
- Broker stopped at the end (`scripts/kafka.sh down`), no java process of ours left.

## Bus-semantics differences found (documented, no invariant weakened)
1. Cross-product order for one vehicle on events.resolved is the crosswalk's consumption order on Kafka
   (raw is keyed per product id, so a vehicle's telematics and maintenance events sit on different raw
   partitions). Per (product, native id) order and the resolved stream's per-vehicle order (identical for
   every consumer) hold. SqliteBus had a total order. No scenario depends on it (A52).
2. Offsets are delivery tokens, not stable positions (A53); `messages()` orders across partitions by produce
   timestamp (only used for observation and tests).
3. `reset_group` supports offset 0 (earliest) only.
4. Retention is 7 days: replay further back is impossible on Kafka (A55).

## Decisions needed from the owner
1. Consumer-group membership (A51): manual assignment (one active reader per group, built) vs subscribe +
   rebalance for scale-out across processes.
2. Whether cross-product per-vehicle order matters (A52); if it does, ordering has to come from event time
   in the workflow, not the bus.
3. JDK for a real broker (A56): this ran on JDK 24; Kafka's documented JVMs are 17/21.
4. Retention (7 days) vs the replay window the audit story needs (A55).

## Not done
No TLS/SASL, no multi-broker/replication > 1, no schema registry, no dynamic rebalancing, no Postgres
profile. The demo (`make demo`) is not wired to a Kafka-specific target (it runs on Kafka with
`TRANSITOS_BUS=kafka`).
