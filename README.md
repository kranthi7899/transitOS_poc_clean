# TransitOS orchestration POC

A proof of concept for **cross-product agentic orchestration** in a transit agency, built at autonomy **Level 2: act with approval**. The agent proposes, a dispatcher approves, and only then does anything get written.

> **Every product in this repo is simulated.** The five mock products are modeled on the shapes of public documentation. They are not the real products, do not call them, and must never be presented as them. All data is synthetic.

## Explore the project interactively

Run **`make explorer-serve`** and open **http://127.0.0.1:8111** for a clickable architecture map, source-linked guide, learning path, evaluation evidence and dispatcher replay. The replay includes approval, rejection, repair and a separate return-to-service approval, captured from the actual integrated workflow. It uses no API credits.

This original explorer takes inspiration from [OpenWiki](https://github.com/dakjdakd/openwiki). GitHub READMEs cannot run its JavaScript, so the interactive experience lives in `site/`, ready for GitHub Pages. Hosting is not yet verified; [hosting instructions](docs/EXPLORER.md#hosting) cover the prepared manual deployment workflow. No screenshots are needed.

For actual workflow execution, run **`make console`** and open **http://127.0.0.1:8110**. The console shows Needs Attention, human approvals, actual LangGraph activity, product state, audit and incident memory. [Demo instructions](docs/DEMO.md) explain the recording sequence.

## The scenario

A bus reports a serious engine fault. Today, a person has to notice it, open a work order in the maintenance system, pull the bus in the scheduling system, find a spare and a qualified operator, reassign the block, and tell riders. TransitOS does that as one governed workflow:

```
vehicle fault -> work order -> pull vehicle -> reassign block (+ operator if needed)
              -> rider alert -> repair -> return to service
```

The agent never writes until a human approves a concrete plan. The rider alert is always the last write, so riders are never told about a change that did not happen.

## Design principles

| Principle | How it is enforced |
|---|---|
| Hard limits are code, never model output | Last spare, operator hours, vehicle qualification and lift needs are checked in code before the plan is shown, and again at write time against live product state. |
| Writes are idempotent | Key = `<run_id>:<step>:<plan_version>`. A retried or duplicated write has no second effect. |
| Every write has a compensation | A failure mid-plan runs the saga in reverse (one undo per pass), or escalates with the exact state. |
| Rider alert is last | It is the final step of the plan, and nothing is undone after an alert attempt. |
| No write inside an interrupt | A node that waits for a human performs no write, so a resume that re-runs the node is safe. |
| Kill switch | Per agency, checked on every write. A blocked write is never retried. |
| Append-only audit | Every revision of every decision is stored, with the model, the human decision and the idempotency keys. |
| The LLM is replaceable | All reasoning goes through one interface in `workflow/reasoning.py`. The default is a stub plus a code-ranked fallback. The dispatcher demo supports OpenAI; the legacy Haiku adapter remains optional. |

## Architecture

```
 mock products (FastAPI, shared simulated clock)
   telematics  --signed webhook--> receiver ┐
   telematics  /faults/feed -----> poller   │  watch adapters: cursor + last-seen + outbox
   maintenance /pm-schedules, /workorders   ├-> rows in ONE transaction
   scheduling  /status-changes              │
   workforce   /availability-changes        ┘
        -> outbox -> relay -> bus            events.raw       (CloudEvents, key = <product>:<native id>)
        -> crosswalk (native ids -> canonical veh-/op- ids)
              resolved:  events.resolved
              unknown:   parked + events.failed  (panel lists them, re-publishes once mapped)
        -> workflow consumer: dedupe, start / attach / resume / signal
        -> LangGraph run (checkpointed):
             gather (reads) -> check (hard limits, code) -> reason (stub, validated)
             -> policy -> propose -> await_approval (interrupt, no write)
             -> revalidate -> execute steps (one write per pass)
             -> wait_for_repair (interrupt) -> return_gate (interrupt)
             -> return_to_service -> close
             on failure: compensate in reverse, or escalate
        -> tool layer: kill switch, idempotency store, live hard-limit re-check, product call, read-back
 dispatcher panel (FastAPI): approvals, defects, unresolved ids, kill switch, timeline
```

### Seven building blocks ("tracks")

| Track | Directory | What it is |
|---|---|---|
| Contracts | `src/transitos/contracts/` | Frozen pydantic models (source of truth). JSON Schema is exported to `contracts-schema/`. |
| A | `src/transitos/mocks/` | Five simulated products, a seeded synthetic fleet, a simulated clock, and chaos modes. |
| B | `src/transitos/plumbing/` | Watch adapters, outbox and relay, event bus, crosswalk, append-only audit. |
| C | `src/transitos/tools/` | Per-agency tool registry, read and write adapters, idempotency, kill switch, hard-limit re-check. |
| D | `src/transitos/workflow/` | The LangGraph workflow, plan model, saga, timers, reasoning interface. |
| E | `src/transitos/panel/` | The dispatcher panel (approve, reject, edit, kill switch, unresolved ids). |
| F | `src/transitos/verification/` | Scenarios, invariants, and the report. |

**Added in Phase 10:**
- `src/transitos/clearance/`: the one pure module for every hard limit and graded rule. Used by the planner, the plan validator and the write adapters, and it fails closed on any error. Vehicle fit is graded (exact type is best; a larger class is a warning that needs approver acknowledgement; a smaller class is never a candidate). Missing data is never a violation and never a silent pass: each rule reports whether its data was native, derived, assumed or unknown, and unknown safety rules are "unverifiable" and need acknowledgement. The plan shows its top three options and why the first beat the second.
- `src/transitos/plumbing/resolver.py`: an identity resolver for messy ids (VIN with check digit, plate, fleet number) over a validity-dated crosswalk, with MATCH, AMBIGUOUS (a human picks) and NO_MATCH outcomes.
- `src/transitos/world/`: a scripted-then-seeded ground truth that each simulated product sees after its own delay, plus a simulated shop foreman (pending repair, parts wait) and optional zone-less local timestamps.
- `src/transitos/data/` and `make data`: an offline-testable pipeline that fetches public NTD data once, verifies it, and derives three small committed files (real bus failure rates, fleet mix, repair-duration shape). Raw data is never committed.

`src/transitos/integration/` wires everything into one `System`, with a narrated demo. Design notes live in `docs/` (`ARCHITECTURE.md`, `CONTRACTS.md`, `MOCK_API.md`, `ASSUMPTIONS.md`). Handoff notes per track are in `coordination/handoffs/`.

## The mock products and the mock data

Five simulated products, each a FastAPI app with a `/sim` admin surface to seed data, trigger faults, advance the clock and inject failures.

| Mock | Modeled on (public docs) | Role | Notable behavior |
|---|---|---|---|
| Telematics | Samsara-style | Fault codes (SPN/FMI, lamps), DVIR defects | Signed webhook (HMAC-SHA256) plus a REST feed. Duplicates and late delivery under chaos. |
| Maintenance | FASTER-style | PM schedules, work orders | Polled. Accepts an idempotency key. Rate-limits (429) under chaos. |
| Scheduling | TripShot-style | Vehicles, blocks, status | No idempotency keys, and no endpoint to cancel a block. Rejects a reassign if the spare lacks the needs. |
| Workforce | CSV-export style | Operators, hours, qualifications | Reads come from a daily CSV. The assign write can fail. |
| Rider alerts | GTFS-Realtime service-alert style | Rider-facing alerts | Eventually visible: an alert appears only after a delay on the simulated clock. |

**Each product uses its own ids.** One bus is `BUS-44` in scheduling, `T-77` in telematics, and asset `1106` in maintenance. Operators are `OP-310`. Only the crosswalk maps these to canonical ids (`veh-1`, `op-1`), which is the cross-product identity problem the POC is built to exercise.

**Synthetic fleet** (`src/transitos/mocks/seed.py`, deterministic):
- 12 vehicles. 9 in service (standard, cutaway, articulated, some with no wheelchair positions or no lift) and 3 spares (one lift-capable standard, one small cutaway without a lift, one standard without a lift). Vehicle 3 is 100 miles from a PM becoming overdue.
- 10 operators with hours today and this week, availability windows, lift training and licence codes. One is close to the daily hour limit, and one is on the extra board.
- Two agencies with different id formats (`agency-042`, `agency-077`) to prove the crosswalk. Custom fleets can be registered for test scenarios.

**Chaos modes** per mock: `timeout`, `http_429`, `duplicate`, `late`, `reject_write`.

Full HTTP shapes are in `docs/MOCK_API.md`.

## Dispatcher browser demo

`make console` starts the free rehearsal at **http://127.0.0.1:8110**. `make console-openai` uses the OpenAI key in `.env.local` with a persistent **$0.50** app ceiling. Needs Attention, approvals, actual LangGraph activity, five product lanes, repair/return and audit are visible in one scenario. See [docs/DEMO.md](docs/DEMO.md) for the launch and recording sequence. All products remain simulated.

## Run it

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```
uv sync
make demo      # narrated happy path through the real wiring
make verify    # schemas + lint + tests + the 50-scenario report + offline data check
uv run python -m transitos.mocks.run   # serve the five mocks on localhost:8101-8105
```

`make demo` walks one run: the fault arrives, the plan is proposed with zero writes made, the dispatcher approves in the panel, five writes land in the mock products, the simulated clock jumps 48h and the shop closes the work order, return to service is approved separately, and the audit trail is printed.

### Storage and bus profiles

The same interfaces run in three profiles:

| Profile | Command | Needs |
|---|---|---|
| Local (default) | `make verify` | Nothing. SQLite plus an in-process bus. |
| Kafka | `make kafka-up` then `make verify-kafka` | A JDK. Kafka 4.3.1 is downloaded and checksum-verified into `.local/`. No Docker. |
| Postgres | `make postgres-up` then `make verify-postgres` | Project-local PostgreSQL under `.local/`. No Docker. |

Run `make kafka-down` / `make postgres-down` when finished. Project paths containing a space are handled by `scripts/kafka.sh`, because Kafka's own scripts do not quote paths.

## Optional model reasoning

The default stub costs nothing. `make console-openai` uses OpenAI with a persistent **$0.50 cumulative application cap**, checked before each request. Keep `OPENAI_API_KEY` in the ignored `.env.local` or environment. Never put it in the frontend, README or Git history. The organization spend limit does not add prepaid credits. `make openai-spend` shows the local ledger. The legacy Haiku adapter remains optional. See [LLM.md](docs/LLM.md).

## Agent memory

Coding agents use [AGENTS.md](AGENTS.md) and [the context map](docs/AGENT_MEMORY.md) to recover project decisions and verification commands. Runtime agents retain bounded successful incident summaries in ignored local SQLite storage, scoped to the same agency and vehicle. Memory survives demo resets, is visible in the console, and is never exported into the static site. It supplies historical context; every new proposal still needs fresh facts, code safety checks and human approval.

## Tests and behavioral evals

```sh
make verify      # contracts, lint, unit/integration tests, 50 scenarios, data checks
make evals       # eight focused behavioral evals; zero model calls
make explorer    # evals + regenerate source guide and recorded replay
```

The focused evals exercise the complete approval loop, rejection, stale approvals, missing safety evidence, adversarial model output, scoped memory, spend ceiling and kill switch. They evaluate application behavior; they do not establish live model quality or production readiness.

## Verification

Track F runs 50 scenarios against the real integrated system, checking invariants after each (no write before approval, rider alert last, hard limits never violated, idempotent duplicates, compensation on failure). Scenarios that depend on product lag or wire formats are marked real-world-only and skipped on the stand-in world instead of faking agreement. Two scenarios are recorded as **known gaps**, not clean passes (the orchestrator cannot know a fault has cleared).

Historical Phase 10 baseline (before the console/explorer additions): lint clean, 50/50 scenarios on all three profiles. Current local verification (2026-10-13): **1713 passed, 181 skipped in 78.18s; 50/50 scenarios; lint clean; data-check OK**. The eight focused behavioral evals also passed with zero paid calls. Kafka/Postgres have not been rerun for these additions.

| Profile | Tests |
|---|---|
| Local | 1687 passed, 180 skipped (Kafka and Postgres cases skip without a server) |
| Kafka | 1736 passed, 131 skipped |
| Postgres | 1776 passed, 91 skipped |

Scenario families:
- **Original 24:** severe fault with a spare, operator swap, PM overdue (urgent and minor), DVIR defect, last spare, wheelchair and lift mismatches, operator hours, unqualified operator, duplicates, human work order already exists, unresolved vehicle id, product down on read, approval rejected and expired, compensation, kill switch, spare taken after proposal.
- **Clearance (8):** expired training, missing endorsement, spare reused across blocks, weekly hours cap, larger-class fallback (needs acknowledgement), length restriction blocking a larger vehicle, fleet with half the operator fields missing, `vehicle_fit: exact`.
- **World engine (5):** a scripted coolant-fault day, an intermittent fault, an operator call-out with the extra board, and the dispatcher acting before the human approves.
- **Identity (8):** device swap, ambiguous match (human picks), renumbered asset, dirty ids, orphan telematics unit, retired asset blocks a work order, retired spare never proposed.
- **Shop and time (4):** parts wait stays in wait-for-repair, a human opens the work order first, zone-less local time, the daylight-saving fall-back hour.

## Known gaps

- No reconciliation for partial writes, no rider-alert correction, no authentication on the panel, a single-threaded driver, and no production authentication.
- The orchestrator cannot know a fault has cleared (the telematics mock sends no clear event), so it pulls a bus whose fault already cleared. The detection-latency numbers restate scripted delays; they do not measure how fast the orchestrator reacts.
- A close stamped in the second 01:30 of a daylight-saving fall-back, with no offset in the product's data, is recorded an hour early.
- Many assumptions are still open. They are listed with the question to ask each real product owner in `docs/ASSUMPTIONS.md` (including decisions A197–A204 for the console, explorer and memory).

## Conventions

Contracts are frozen, and changes go through a request file in `coordination/requests/`. "Done" means `make verify` output, not an assertion. No client names or client documents belong in this repo.
