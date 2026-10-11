# Assumptions register (doubles as Phase 0 question list for the client)

Consolidated by integration (2026-10-10). The six tracks each numbered their own assumptions from A11, so
ids collided; every assumption now has one id (old track ids in the mapping table at the end). Status:
**open** = still assumed, needs the real product owner; **confirmed** = verified against the built system
(tests/integration) and kept; **resolved** = the question went away (shape agreed / code changed);
**decision** = needs a program/client decision, current behaviour stated.

## Product and domain (questions for the real product owners)

| ID | Assumption | Where | Status | Question for the real product owner |
|---|---|---|---|---|
| A1 | Maintenance product accepts an Idempotency-Key header | maintenance mock, C | open | Do FASTER writes support an idempotency key? |
| A2 | Scheduling has no write endpoint for pulling a vehicle | scheduling mock | open | Is there a write API for vehicle removal in Ecolane/TripShot? |
| A3 | Driver hours/qualifications only via daily CSV | workforce mock, C | open | Which system holds operator hours and qualifications? |
| A4 | LangGraph re-runs the interrupt node from its start on resume | workflow | confirmed (langgraph 1.2.14, tests/workflow/test_a4_interrupt.py) | - |
| A5 | Scheduling is the system of record for vehicle availability | workflow | open | Which product owns vehicle availability? |
| A6 | Operator max hours/day = 10 | policy | open | Agency-specific labor rules? |
| A7 | PM urgent threshold = 1000 mi overdue; below it the run is `monitor` (rule in code, before any read) | policy, D | open | Agency PM policy? |
| A8 | Telematics webhook signature scheme: HMAC-SHA256 over the raw body, header `X-Signature: sha256=<hex>` | telematics mock, B | open | Real auth scheme? |
| A9 | One active run per vehicle; further start events attach as context | workflow | open | Operational reality? |
| A10 | Local profile (SQLite + in-process bus) is behaviourally equivalent to Kafka/Postgres for the POC | plumbing | open (internal) | n/a |
| A11 | Telematics `eventId` is per occurrence; `occurrenceCount` grows per vehicle+spn+fmi until cleared; vendor `eventId` is identical on webhook and feed (reconciliation key) | A, B | open | Does the vendor id stay stable across webhook retries and the REST feed? |
| A12 | DVIRs arrive on the same webhook/feed as `{eventId, vehicle, dvir:{defect_text, reported_by_role}, time}` | A, B | resolved (B adapted to A's shape; tests/integration) | Real DVIR delivery channel and shape? |
| A13 | A cancelled system WO reads `status=closed` + `cancelled:true`; it is not a repair (no WorkOrderClosed) | A, B, C | resolved (poller skips it; tests/integration) | Real cancel semantics/status? |
| A14 | Block `needs.type` is a minimum vehicle class (cutaway < standard < articulated) in the product; the workflow and adapters require an exact type match | A, C, D | open (inconsistent strictness, see gaps) | How are vehicle-type substitutions decided? |
| A15 | Late telematics events become visible after a sim-clock advance plus any request/flush | A | open (mock behaviour) | n/a |
| A16 | Chaos is per product mock; `http_429` affects maintenance only | A | open (mock behaviour) | Real rate limits per product? |
| A17 | `updated_since` / `since` filters are inclusive (`>=`); consumers dedupe by deterministic event id | A, B | confirmed (tests/integration) | Real filter semantics and clock skew? |
| A18 | `/availability-changes` items are `{operator, status, changed_at}` | A, B | confirmed | Real feed shape? |
| A19 | PMOverdue fires on the transition to `meter_mi > due_mi` per (asset, service_code, due_mi); WorkOrderOpened on first sight of a WO (also if first seen closed) | B | open | Does the maintenance product expose PM state changes directly? |
| A20 | Maintenance asset ids map to canonical vehicles through the same crosswalk (`maintenance`, entity `asset`) | A, B, C | confirmed | One asset per vehicle? |
| A21 | There is no `GET /blocks/{id}`; adapters list `/blocks` and filter | C | open | Real block lookup API? |
| A22 | Rider alerts are a product key `alerts`; header text = first line of the message, max 100 chars | C | open | Real alerts product and field limits? |
| A23 | Native status strings: ready/available -> ready; in_service/active -> in_service; out_of_service/unavailable -> out_of_service; reserved; WO cancelled -> closed; unknown -> unavailable (never guessed) | C | open | Real status vocabularies? |
| A24 | Crosswalk product keys: scheduling, telematics, maintenance (entity asset), workforce (operator); block and WO ids are native pass-through | C, B | confirmed | - |
| A25 | min_spares counts ready vehicles without an overlapping block in the window (target included); using one must leave >= min_spares | C, D, F | confirmed (same rule in C recheck, D check, F invariant) | Agency definition of a usable spare? |
| A26 | Operator qualification for a block = `block.needs.type in operator.qualifications` (D's hard limit, F's invariant); the C adapter does not check qualifications | C, D, F | decision (see gaps: the agency-042 seed uses licence codes CDL-B/PAX/ARTIC, so no operator qualifies there) | What qualifies an operator for a vehicle type? |
| A27 | Workforce is the system of record for who drives a block (`GET /assignments/{block}`); scheduling's `operator` field is not updated by an operator reassignment | A, C | open | Which product owns operator-to-block assignment? |
| A28 | Workforce availability window is RFC3339 on the export day | A, C | resolved (mock renders RFC3339; it served `HH:MM`, which C could not parse) | Real CSV format? |

## Workflow, panel and verification (internal design assumptions)

| ID | Assumption | Where | Status |
|---|---|---|---|
| A29 | Resolved events carry `data["refs"]` (VehicleRefs) / `data["operator_canonical"]` and envelope `canonicalid` | B, D, contracts | resolved (contracts v1.1; D used `data["vehicle"]["canonical"]`) |
| A30 | Update events caused by our own writes are ignored (`opened_by=system`, `changed_by` starts with `transitos`) | D, C, A, contracts | resolved (scheduling writes now send `changed_by=transitos:<key>`; mock records it; tests/integration) |
| A31 | `assign_operator` is a plan step only if the block's current operator is missing or fails a hard limit | D, C, F | confirmed (C now lists the current operator when eligible; F's test_01 uses `operator_swap_needed`) |
| A32 | A hard-limit violation in `check` escalates before the model is called; any read not `ok` (incl. partial) escalates, never guessed | D | confirmed (tests + scenarios) |
| A33 | Kill switch is read live before every write and every compensation; on trip, halt and escalate with exact state, no auto-compensation | D, C | confirmed |
| A34 | Per-step autonomy: `act` steps run before approval; the first `approve` step triggers one approval for the rest | D, F | open (only `approve` exercised end to end) |
| A35 | Stale before any write -> plan_version+1 (fresh idempotency keys), re-reason, new approval; max 3 replans. Stale/expired/edit after writes -> compensate, escalate | D | confirmed for the before-write path (scenario `spare_taken_after_proposal`) |
| A36 | Write retries: 3 attempts with the same key; `partial` (sent, read-back failed) is not retried and is recorded done + `unverified`; blocked-by-policy results are never retried | D, C | open (no reconcile for partial) |
| A37 | Terminal runs release the vehicle's active slot | D | open |
| A38 | WorkOrderClosed arriving before the run waits for repair is parked and applied at the gate; return-to-service is its own approval (`autonomy.return_to_service`), no timeout | D, F | confirmed (panel has a `return_to_service` decision scope) |
| A39 | Crosswalk has no listing in the contract; the panel reads unresolved ids through an `UnresolvedReader` port | E | confirmed (integration implements it over SqliteCrosswalk) |
| A40 | Decision staleness is checked in the panel and again in the gateway; double submit of the same decision is idempotent | E | confirmed |
| A41 | Edit decisions carry `{"assignments":[{block_id, spare_id, operator_id}]}` | E, D | open (no end-to-end edit through the real world) |
| A42 | No panel authentication; `user` is self-reported | E | open |
| A43 | Outcome classification from contract fields: `RunState.violations` prefixes `kill_switch`, `product_unavailable`; others = hard limit | F, D | resolved (D's escalate node now writes these tags) |
| A44 | The 12 acceptance tests are derived from CLAUDE.md + briefs (the spec is not in the repo) | F | open (map to the real spec); test_01 moved to `operator_swap_needed` (see INTEGRATION.md) |
| A45 | Compensations are exempt from approval but not from the kill switch | F, D | confirmed |
| A46 | `autonomy=recommend` forbids every write of that action; `act` exempts approval | F, D | open (not exercised on the real world) |
| A47 | A failed rider alert (irreversible) is escalated without compensating earlier steps | integration, D | decision (current behaviour; forced by F's rider_alert_last invariant) |
| A48 | After a failure that follows `reassign_block`, strict reverse undo stops: scheduling refuses to put the block back on the out-of-service vehicle; the run escalates with exact state | integration | resolved by A57 |
| A49 | Verification scenarios and the demo run on the scenario baseline fleet (agency `agency-a`, vehicle type `standard`) seeded into the real mocks; the agency-042 seed currently escalates (A26, one spare per block) | integration | decision |
| A50 | The integration operations log (human decisions, expiries, kill-switch toggles with a global sequence) is in memory; the durable audit is the append-only DecisionStore | integration | open |

## Old-id mapping

| Track | Old id | New id |
|---|---|---|
| A | A11 DVIR on webhook / A12 cancel = closed+cancelled / A13 eventId per occurrence / A14 type minimum class / A15 late delivery / A16 chaos scope | A12 / A13 / A11 / A14 / A15 / A16 |
| B | A11 PM transition + WO first sight / A12 availability item shape / A13 DVIR `defect` shape / A14 vendor eventId stable / A15 inclusive since / A16 asset ids via crosswalk | A19 / A18 / A12 (superseded) / A11 / A17 / A20 |
| C | A11 no GET /blocks/{id} / A12 alerts product key / A13 status mapping / A14 crosswalk keys / A15 min_spares / A16 qualifications not matched | A21 / A22 / A23 / A24 / A25 / A26 |
| D | A11 canonical shape / A12 self-inflicted / A13 assign only if needed / A14 escalate before model / A15 kill switch / A16 per-step autonomy / A17 stale replan / A18 write retries / A19 release slot / A20 early WO close | A29 / A30 / A31 / A32 / A33 / A34 / A35 / A36 / A37 / A38 |
| E | A11 UnresolvedReader / A12 staleness / A13 edits shape / A14 kill switch elsewhere / A15 no auth | A39 / A40 / A41 / A33 / A42 |
| F | A11 classification / A12 qualification + spare rule / A13 derived tests / A14 return-to-service exempt from alert-last / A15 autonomy recommend/act | A43 / A26 + A25 / A44 / A38 + A45 / A46 |

## Kafka profile (appended 2026-10-10, see coordination/handoffs/KAFKA.md)

| ID | Assumption | Where | Status |
|---|---|---|---|
| A51 | KafkaBus readers assign themselves all partitions of a topic and commit under the group id (manual assignment); one active reader per (topic, group). No dynamic rebalancing between processes of one group: the POC driver is single-process, and a crashed member would otherwise hold its partitions until the session timeout | plumbing/kafka_bus.py | decision (scale-out needs subscribe + rebalance handling) |
| A52 | On Kafka, the order of one vehicle's events from DIFFERENT products on events.resolved is the crosswalk's consumption order: events.raw is keyed `<product>:<native id>`, so the two products' events sit on different partitions. Order per (product, native id) and the resolved stream's per-vehicle order (same for every consumer) are guaranteed. SqliteBus happened to give a total order. No scenario depends on cross-product order | plumbing, workflow | open |
| A53 | Kafka offsets surfaced through the bus protocol are per-reader delivery tokens, comparable within one process only; the durable position is Kafka's committed offset per partition. `audit_raw/audit_resolved.bus_offset` hold the token on Kafka (informational) | plumbing/kafka_bus.py | decision (contract unchanged) |
| A54 | Each System on Kafka uses its own topic/group namespace (stored in its data dir); canonical names only with `TRANSITOS_KAFKA_NAMESPACE=none`. Test namespaces are deleted by `scripts/kafka.sh purge` | integration/system.py | decision |
| A55 | Kafka topics keep 7 days (retention.ms); replay from earlier than that is impossible on Kafka (SqliteBus keeps everything) | scripts/kafka.sh, kafka_bus.py | open |
| A56 | Kafka 4.3.1 is run on the machine's JDK 24 (the brief lists 17/21 as Kafka's supported JVMs); it started and passed every test here. A deployed broker should use a JDK Kafka lists as supported | scripts/kafka.sh | open |
| A57 | Compensating `reassign_block` is a restore of the prior assignment: the scheduling mock accepts it (via the compensation flag) even when the previous vehicle is out of service, but still refuses normal assignment to an out-of-service vehicle. Resolves A48 | mocks/scheduling.py, tools/write_adapters.py | decision (owner has not chosen a rule) |
| A58 | The scheduling mock tracks only the single previous vehicle per block; a restore after two reassigns is not covered | mocks/scheduling.py | open |
| A59 | "Available again" after undo means the mock's `ready` status; the test treats `in_service` and `ready` as equal | tests/integration | open |
| A60 | The Postgres profile stores each SQLite file's content in its own schema `<namespace>_<store>`; per-System namespaces are persisted in the data dir, canonical names only with `TRANSITOS_PG_NAMESPACE=none`; test schemas are dropped by `scripts/postgres.sh purge` | integration/system.py, plumbing/pg.py | decision |
| A61 | SQL stays written once in the SQLite dialect and is translated for Postgres by `plumbing/pg.py`; the shared contract suite (`test_storage_contract.py`) is what keeps both backends identical. New SQL in plumbing/workflow/tools must stay inside the translator's vocabulary or extend it | plumbing/pg.py | decision |
| A62 | `PgRunsIndex` reuses `workflow.store.RunsIndex` and its private `_SCHEMA`, and `PgIdemStore` reuses `tools.idem_store._TYPES`; changes there are picked up automatically, but renaming those private names breaks the Postgres profile | plumbing/pg_stores.py | open |
| A63 | Local Postgres is the zonky.io build of upstream PostgreSQL 17.7 (x86_64 binary under Rosetta on Apple Silicon), trust auth on 127.0.0.1 only. A deployed database needs authentication, TLS, backups and a supported PostgreSQL build | scripts/postgres.sh | open |
| A64 | No connection pooling or reconnect: one connection per store per System; a server restart needs `System.restart()` | plumbing/pg.py | open |

## Phase 10: external data pipeline (A115-A124)

Added at integration from `docs/assumptions/phase10-data.md`. Observed facts from the real fetch are in `data/FETCH_REPORT.md`.

| ID | Assumption | Where | Status | Real source to check / question for Transit Technologies |
|---|---|---|---|---|
| A115 | The `fixtures/j1939_spn_fmi.yaml` rows (about 15 SPNs, names in our words, system mapping, units, typical FMIs) are plausible for transit buses. Names marked `unverified` were not seen in a primary source in this session. | `fixtures/j1939_spn_fmi.yaml` | open | The licensed SAE J1939 digital annex; which SPN/FMI pairs the agency's telematics unit actually emits (most common pairs on transit buses: UNKNOWN). |
| A116 | Severity of a fault code is a configurable rule (per-FMI default plus a few SPN/FMI overrides, levels monitor < service_soon < pull_vehicle), not ground truth. | `fixtures/j1939_spn_fmi.yaml` `severity_rule`, `loaders.J1939.severity` | open | How does the agency decide a code is pull-now vs. service-soon? OEM guidance per engine. |
| A117 | Weights across vehicle systems for generated faults (brakes 0.10, doors 0.10, engine cooling 0.10, ..., sum 1.0). No published split exists. | `config/fault_mix.yaml` | open | The agency's own road-call log by category; NREL/FTA zero-emission bus evaluation reports (not yet opened). |
| A118 | The category list (FTA major-failure examples plus common road-call categories plus a general engine bucket) is sufficient, and each system's `primary_spns` mapping is a fair link between a system and the fixture SPNs. | `config/fault_mix.yaml` | open | The agency's work-order failure-code taxonomy. |
| A119 | Shop behaviour fallback: pending-to-work-order delay, 25 percent parts-wait, 10 percent deferral back to pending, repair hours by severity, all lognormal with hand-set medians/sigmas. | `config/shop_behaviour.yaml` | open | Real foreman conversion delays and parts-wait rates (published transit-bus figures: UNKNOWN). The Cincinnati-derived shape in `data/derived/repair_durations.yaml` is a cross-check, not a source of these numbers. |
| A120 | Model year minus manufacture year is 0 for 70 percent of mock vehicles and 1 for 30 percent. The NTD inventory has `Manufacture Year` and `Rebuild Year` but no model-year column (observed), so the gap cannot be measured from it; the glossary only says a model-2013 vehicle was likely made in 2012. | `data/derived/fleet_mix.yaml` `model_year_gap` | open | VIN model-year code versus build date for real fleets; the NTD glossary. |
| A121 | A row with ANY non-empty flag on the failures column or the revenue-miles column is excluded from the failure-rate distribution. Observed flag values are `Q` and `W`; the dataset metadata documents only "Questionable" and does not explain `W`, so `W` is excluded conservatively. | `transitos.data.derive.derive_fault_rates` | open | NTD data dictionary: meaning of flag value `W`. |
| A122 | Fleet-level rows (agency x mode x type of service x year) stand in for per-vehicle reliability: miles between major failures = revenue miles / major mechanical failures of the whole fleet in that row. Quantiles are across agency-years, so they describe the spread between fleets, not between individual buses. Using a quantile as the Poisson mean for one bus is a modelling choice. | `data/derived/fault_rates.yaml` | open | Per-vehicle failure history from an agency. |
| A123 | Blank or missing make/model/length/fuel in the NTD inventory is kept as `(blank)` and is not imputed. Make and model strings are not merged across spelling and case variants (for example `Low Floor` vs `LowFloor`). Vehicle length is presumed to be feet (values such as 40); the unit is not stated in the file. | `data/derived/fleet_mix.yaml` | open | NTD 2024 file dictionary (linked from the landing page, not downloaded). |
| A124 | The Cincinnati work-order data (general city fleet, whole-day timestamps, public domain per the dataset metadata) is used for the SHAPE of repair durations only (skew, long tail, share of zero-day jobs), never for absolute values for transit buses. | `data/derived/repair_durations.yaml` | open | Transit-bus work-order history. |
