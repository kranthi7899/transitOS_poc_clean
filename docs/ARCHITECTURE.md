# Architecture (as built, local profile)

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
        -> outbox -> Relay -> SqliteBus  events.raw  (key <product>:<native id>)
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
| Event plumbing (B) | Watch adapters, outbox/relay, bus with replay, crosswalk, audit | As designed; SqliteBus implements Producer/Consumer (per-key order, at-least-once, consumer offsets, replay) | KafkaBus is a stub |
| Tools (C) | Per-agency registry, adapters, hard limits in code, idempotent writes, kill switch | As designed; blocked writes `Result.blocked`; block reads overlay the workforce assignment | Adapter re-check covers needs/spares/hours/lift, not qualification (A26) |
| Workflow (D) | LangGraph, interrupts without writes, saga, approval, timers, reasoning interface | As designed + PM urgency rule in code, escalation outcome tags, no undo after a rider-alert attempt | One spare per block (A49); undo order decision (A48) |
| Panel (E) | Approvals, defects, unresolved ids, kill switch, timeline | As designed + return-to-service decision scope, sim-clock injection | No auth (A42) |
| Verification (F) | Scenarios, invariants, report, World | 23 scenarios on FakeWorld and on the real integrated world | `World.restart` added on the integration world only |
| Integration | Wire everything | `src/transitos/integration/`: `System` (tick/run_until_idle/restart/recover), panel ports, write log, `IntegrationWorld`, demo | Deterministic single-threaded driver instead of background loops |

## Local profile vs production mapping

| Concern | Local profile (this repo) | Production profile (design, not built) |
|---|---|---|
| Bus | `SqliteBus` (one SQLite file, global offset, consumer groups) | Kafka (`KafkaBus` stub, same Producer/Consumer protocol) |
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
make verify      # schemas + lint + tests + report
uv run python -m transitos.mocks.run   # serve the five mocks on localhost:8101-8105 (manual poking)
```

In code: `IntegrationWorld().apply(BY_ID["severe_fault_with_spare"])` gives a seeded system at
`world.system` (`.tick()`, `.run_until_idle()`, `.restart()`, `.panel_client`, `.writes.records`).
`System(agency, Mocks.start(SimClock()))` wires the same thing for any seeded agency.

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
- No Kafka/Postgres profile, no authentication, no real LLM, single-threaded driver (no concurrent resumes
  of one run).
