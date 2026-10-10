# Architecture (as built: local profile, Kafka profile)

Simulated-products POC of cross-product agentic orchestration at Level 2 (act with approval):
vehicle fault -> work order -> pull vehicle -> reassign block (+ operator if needed) -> rider alert ->
repair -> return to service. The five products are SIMULATED mocks modeled on public docs; they are not the
real products.

## Data flow

```
 mock products (src/transitos/mocks, FastAPI; shared SimClock)
   telematics --signed webhook--> WebhookReceiver ┐
   telematics /faults/feed  ---> backfill poller   │   (B) watch adapters: cursor + last-seen + outbox
   maintenance /pm-schedules,/workorders -> poller ├─> rows in ONE SQLite transaction
   scheduling /status-changes -> poller            │
   workforce /availability-changes -> poller       ┘
        -> outbox -> Relay -> bus (SqliteBus | KafkaBus)  events.raw  (key <product>:<native id>)
        -> CrosswalkService (SqliteCrosswalk, seeded from mocks.crosswalk_seed)
             resolved: events.resolved (key canonical id, data.refs, canonicalid)
             unknown id: parked + events.failed  (panel /unresolved -> /crosswalk republishes)
        -> workflow consumer (integration) -> starter.handle_event (D): dedupe, start/attach/resume/signal
        -> LangGraph run (D, SqliteSaver checkpoints): gather (C reads) -> check (hard limits, code)
             -> reason (StubReasoner, validated; code-ranked fallback) -> policy -> propose
             -> await_approval (interrupt) -> revalidate -> execute_step x N (one C write per pass)
             -> wait_for_repair (interrupt) -> return_gate (interrupt) -> return_to_service -> close
             failure: compensate (reverse, one undo per pass) / escalate with exact state
        -> Track C ToolSet (registry -> adapters): kill switch, idempotency store, hard-limit re-check from
           live product state, product call, read-back  -> mock products
 panel (E, FastAPI) over real ports: RunGateway (graph), EventSink (outbox), AuditReader (B DecisionStore),
   KillSwitchStore (C), UnresolvedReader (B crosswalk)
 audit: B AuditConsumers (raw/resolved tables) + append-only DecisionStore (every DecisionRecord revision)
```

## Components: as built vs design intent

| Component | Design intent (CLAUDE.md, briefs, contracts) | As built | Difference / note |
|---|---|---|---|
| Contracts | Frozen pydantic v1, JSON Schema export | v1.1 additive (docs/CONTRACTS.md): resolved-event `refs`, `canonicalid`, `Result.blocked`, `compensation` keyword, compensation keys, `cancel_work_order` action | Committed schemas are tested against the models |
| Mock products (A) | Five SIMULATED products, chaos, sim clock | As designed + registerable custom fleets, `changed_by` on scheduling writes, RFC3339 workforce windows | `/sim` routes drive repairs, meters, dispatcher actions |
| Event plumbing (B) | Watch adapters, outbox/relay, bus with replay, crosswalk, audit | As designed; SqliteBus and KafkaBus both implement Producer/Consumer (per-key order, at-least-once, consumer offsets, replay); one contract suite runs on both | Kafka offsets are per-reader delivery tokens; see "Kafka profile" |
| Tools (C) | Per-agency registry, adapters, hard limits in code, idempotent writes, kill switch | As designed; blocked writes `Result.blocked`; block reads overlay the workforce assignment | Adapter re-check covers needs/spares/hours/lift, not qualification (A26) |
| Workflow (D) | LangGraph, interrupts without writes, saga, approval, timers, reasoning interface | As designed + PM urgency rule in code, escalation outcome tags, no undo after a rider-alert attempt | One spare per block (A49); undo order decision (A48) |
| Panel (E) | Approvals, defects, unresolved ids, kill switch, timeline | As designed + return-to-service decision scope, sim-clock injection | No auth (A42) |
| Verification (F) | Scenarios, invariants, report, World | 23 scenarios on FakeWorld and on the real integrated world | `World.restart` added on the integration world only |
| Integration | Wire everything | `src/transitos/integration/`: `System` (tick/run_until_idle/restart/recover), panel ports, write log, `IntegrationWorld`, demo | Deterministic single-threaded driver instead of background loops |

## Local profile vs production mapping

| Concern | Local profile (this repo) | Production profile (design, not built) |
|---|---|---|
| Bus | `SqliteBus` (one SQLite file, global offset, consumer groups) | Kafka: `KafkaBus`, built and tested on a local single-node broker (`TRANSITOS_BUS=kafka`, below) |
| State stores | SQLite files: `plumbing.db` (bus, outbox, cursors, crosswalk, audit), `runs.db`, `checkpoints.db` (LangGraph SqliteSaver), `idem.db`, `killswitch.db` | Postgres (LangGraph Postgres saver, same tables) |
| Products | In-process FastAPI mocks via Starlette TestClient (or `python -m transitos.mocks.run` on ports 8101-8105) | Vendor APIs via `httpx.Client` per registry `base_url` |
| Scheduling of work | `System.tick()` called by the demo/tests; `run_until_idle()` | Long-running pollers, relay, consumers, timer service |
| Clock | `SimClock` shared by mocks, workflow, timers | Wall clock |
| Reasoning | `StubReasoner` + code-ranked fallback; no LLM calls | Provider behind `workflow/reasoning.Reasoner` (decision open) |
| Webhook | `WebhookReceiver` app called in-process, HMAC verified | Public HTTPS endpoint, real vendor signature scheme (A8) |
| Panel | FastAPI app in-process (TestClient) or uvicorn | Same app behind auth |

## How to run

```
uv sync
make demo        # narrated happy path through the real wiring (python -m transitos.integration.demo)
make test        # uv run pytest -q   (unit + integration + e2e against FakeWorld AND the real world)
make report      # Track F scenarios against the real system -> verification-report.json
make verify      # schemas + lint + tests + report (SQLite bus; Kafka tests skipped without a broker)
make verify-kafka  # same system on a real Kafka broker (see "Kafka profile")
uv run python -m transitos.mocks.run   # serve the five mocks on localhost:8101-8105 (manual poking)
```

In code: `IntegrationWorld().apply(BY_ID["severe_fault_with_spare"])` gives a seeded system at
`world.system` (`.tick()`, `.run_until_idle()`, `.restart()`, `.panel_client`, `.writes.records`).
`System(agency, Mocks.start(SimClock()))` wires the same thing for any seeded agency.

## Kafka profile (built)

`TRANSITOS_BUS=kafka` (or `System(..., bus="kafka")`) swaps `SqliteBus` for `KafkaBus`
(`src/transitos/plumbing/kafka_bus.py`); everything else (outbox, crosswalk, audit, workflow, SQLite state
stores) is unchanged. The default stays `sqlite`: `make verify` needs no broker and no extra dependency.

| Concern | Local profile (SqliteBus) | Kafka profile (KafkaBus) |
|---|---|---|
| Broker | none | Apache Kafka 4.3.1, single node, KRaft (broker+controller in one JVM), `127.0.0.1:9092` only, from the SHA-512-verified Apache tarball under `.local/` |
| Topics | rows in `bus_messages` | `events.raw`, `events.resolved`, `events.failed`: 3 partitions, replication 1, retention 7 days (`scripts/kafka.sh topics`) |
| Producer | synchronous insert | idempotent (`enable.idempotence`, `acks=all`), murmur2 key partitioning (as the Java client); `flush()` waits for every ack and raises on any failure, so the relay leaves the outbox row unsent |
| Wire format | pydantic JSON in SQLite | CloudEvents structured JSON as the record value, header `content-type: application/cloudevents+json`, record key = contract key |
| Offsets | one global autoincrement offset | per partition in Kafka; `poll()` yields a per-reader delivery token; `commit(token)` commits every partition up to what was yielded before that token (synchronous commit to `__consumer_offsets`) |
| Order | total | per key (= per partition). Cross-product order for one vehicle is the crosswalk's consumption order (raw is keyed per product id), see A52 |
| Redelivery | `poll()` never advances; uncommitted rows come back | same: a poll after an uncommitted poll re-assigns the partitions at the committed offsets |
| Groups | `bus_offsets` rows | Kafka consumer groups (committed offsets per group id); each reader assigns itself all partitions (no dynamic rebalance, A51) |
| Replay | `reset_group(topic, group, 0)` | same call: commits the earliest offsets for the group |
| Isolation | one SQLite file per System | one topic/group namespace per System (`<ns>.events.raw`), stored in `data_dir/kafka-namespace` so `restart()` reconnects; `TRANSITOS_KAFKA_NAMESPACE=none` = canonical names |

How to run (no Docker, nothing installed system-wide; needs a JDK, uses `java_home` or `TRANSITOS_JAVA_HOME`):

```
uv sync --extra kafka      # confluent-kafka (optional extra)
make kafka-up              # download+verify Kafka 4.3.1 into .local/kafka on first use, format, start, create topics
make kafka-status          # pid, RSS, port
make kafka-test            # Kafka-marked tests only (bus contract suite on Kafka + tests/kafka, incl. broker restart)
make verify-kafka          # lint + the WHOLE test suite with TRANSITOS_BUS=kafka + Track F report on Kafka + purge
make kafka-down            # stop the broker (scripts/kafka.sh reset also deletes .local/kafka-data)
```

Kafka-marked tests are skipped unless `127.0.0.1:9092` answers (or `TRANSITOS_KAFKA` is set, which makes
an unreachable broker a failure); the broker-restart tests also need `TRANSITOS_KAFKA_ADMIN=1`.

Measured on this Mac (macOS arm64, 8 GB RAM, JDK 24, heap `-Xmx512m -Xms256m`): broker startup to ready
1.6 s on an empty data dir, 4.7-6.3 s with a few hundred (deleted) test topics in the log; RSS 182-404 MB
right after start, peak RSS 426 MiB (436192 kB, sampled every 1 s across `make verify-kafka`, which
creates ~375 namespaced topics); `.local/kafka` 135 MB on disk, `.local/kafka-data` 17-45 MB. The full
test suite takes ~160 s on Kafka vs ~10 s on SQLite (topic creation and acks per System).

## Known gaps

- The agency-042 seed (the mocks' default fleet) does not complete a run: operator qualifications are
  licence codes (CDL-B/PAX/ARTIC) while the hard limit expects the block's vehicle type (A26), and the
  workflow uses one distinct spare per affected block (8 sequential blocks -> last-spare violation). The
  demo and verification use the scenario baseline fleet (agency-a) on the same mock products.
- Undo order after a failure that follows `reassign_block` (A48, strict xfail): escalates with exact state.
- No reconcile for `partial` writes; no rider-alert correction; approval timeout does not apply to the
  return-to-service approval; edit decisions not exercised end to end on the real world.
- Vehicle type strictness differs: product allows a larger class, adapters and workflow require equality (A14).
- The operations log (approval/expiry/kill-switch ordering) is in memory; durable audit = DecisionStore.
- No Postgres profile, no authentication, no real LLM, single-threaded driver (no concurrent resumes
  of one run). The Kafka profile has no dynamic consumer-group rebalancing (A51) and no TLS/SASL.
