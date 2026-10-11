# Phase 10: realistic fleet and world. Implementation plan (Gate 1 approved with Amendment A)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan wave by wave. Steps use checkbox syntax. The supervisor (program lead session) merges, edits `contracts/`, and runs the Kafka and Postgres profiles; nobody else does.

**Goal:** Test the orchestrator on mess: a fleet that completes a run under realistic qualification and spare rules, identifiers that disagree across products, a scripted-then-stochastic world with per-product delays, and shop and timestamp quirks.

**Architecture:** Four steps in order. Step 1 fixes hard-limit semantics in code. Step 2 adds an identity resolver over a validity-dated crosswalk. Step 3 adds a seeded ground-truth world that products observe with their own lag. Step 4 adds a Pending Repair flow and zone-less local timestamps. A small offline data pipeline derives three committed files from two public datasets.

**Tech Stack:** Python 3.11, pydantic v2, FastAPI mocks, LangGraph, SQLite/Kafka/Postgres profiles, pytest, ruff, uv. New: `rapidfuzz` (core, from Step 2), optional extra `data` (`pandas`, `openpyxl`, `httpx`, `pyyaml`).

**Spec:** `docs/phases/PHASE-10-realistic-fleet-and-world.md` (the build prompt), `docs/phases/PHASE-10-amendment-A.md` (owner decisions; wins on conflict), `docs/RESEARCH_DATA_SOURCES.md`, `data/MANIFEST.yaml`.

## Reconciliation with the earlier draft of this file

The earlier draft (commit `4946d4c`, same session lineage) is superseded. Differences, all resolved in favour of the build prompt:

| Topic | Earlier draft | This plan (follows the prompt) |
|---|---|---|
| Qualification | licence codes mapped to vehicle class, one constant | D-A: licence class + passenger endorsement + trained vehicle types + lift/ramp training, with expiry dates; class mapping in config |
| Spares | 4 h planning horizon | D-C: a spare may serve several non-overlapping blocks; last-spare counts distinct spares (A49 closed) |
| Identity | validity dates + candidates | D-E: `Resolver` protocol, MATCH/AMBIGUOUS/NO_MATCH + confidence, VIN check digit, rapidfuzz, third generated agency |
| World engine | scripted only | scripted first, then seeded stochastic mode from derived NTD failure rates |
| Data | none | offline pipeline (fetch, verify, load, derive) feeding three committed files |
| Kept from the earlier draft | | DST fall-back and spring-forward tests; the Review Focus list below; the A14 decision |


## Amendment A (owner answers at Gate 1; wins over everything above and below)

| Gate-1 question | Decision | Effect on this plan |
|---|---|---|
| A14 vehicle type | Graded, not a wall. Exact type = best tier; larger class = fallback tier (allowed, warning, approver acknowledgement, ranks below every exact plan); smaller class never a candidate; a declared block restriction (for example maximum vehicle length) that a larger vehicle violates is a hard block. Agency setting `vehicle_fit: exact \| allow_larger` (default `allow_larger`), recorded in the decision record. Scheduling mock stays permissive. | Replaces "equality" in task 1.4. New contract fields below. |
| Where rules live | One pure module `src/transitos/clearance/` (owner: Track C lead), called by the planner, write adapters, `validate_output` and verification. No second implementation. Verification keeps only an outcome tripwire (hours cap, minimum spares left, no double-booking) plus a golden table written from the policy text. Fail closed on any exception inside clearance. | Replaces "qualification in `tools/policy.py` + F's own copy". |
| Missing data | No field is assumed. Adapters report a capability profile. Each rule resolves data in order: native, derived, agency default (ASSUMED), unknown. Unknown on an optional rule = `not_evaluated`; unknown on a safety or legal rule = `unverifiable` (stays on the list, ranks below fully verified options, needs approver acknowledgement of that rule). Never a violation, never a silent pass. Everything non-native is shown in the plan, decision record and panel. | Replaces "enforce when fields present". New scenario `half_operator_fields_missing`. |
| NTD links | Hand-set rates tagged ASSUMED if links fail, stated wherever used. | As planned. |
| History push | After this phase. | Local commits only until then; see Risks. |
| Ranking | Hard rules decide the list; rank by unverifiable count, then warnings, then soft terms; keep top three with a one-line reason why #1 beat #2. | New `rank.py`; `Plan.alternatives` carries the runners-up. |

**Rule classification (used by clearance):**

| Class | Rules | Known violation | Unknown data |
|---|---|---|---|
| Hard, safety/legal | vehicle ready; seats, wheelchair, lift capacity; daily and weekly hours cap; licence class, endorsement, trained type, lift training, expiry; availability window; last-spare; kill switch; double-booking; declared block restriction (for example length) | violation (excluded) | `unverifiable` (ack required), except kill switch and double-booking which fail closed |
| Graded | vehicle fit (exact / larger) | larger = `warn` (ack required) under `allow_larger`; larger = violation under `exact`; smaller = excluded | not applicable (type is required) |
| Optional | rest, spread, soft preferences | known breach = `warn`/violation per agency setting | `not_evaluated` |

**New contract fields beyond `D-1` and `B-1` (request file `C-1.md`, all optional):**
- `contracts/clearance.py`: `ClearanceItem(rule: str, status: Literal["pass","warn","unverifiable","not_evaluated","violation"], basis: Literal["native","derived","assumed","unknown"], detail: str = "", requires_ack: bool = False)`.
- `Plan`: `clearance: list[ClearanceItem] = []`, `rank_reason: str = ""`, `vehicle_fit: str | None = None`. `RunState`: `acknowledged: list[str] = []`. `DecisionRecord`: `clearance: list[dict] = []`, `acknowledged: list[str] = []`, `vehicle_fit: str | None = None`.
- `AgencyPolicy`: `vehicle_fit: Literal["exact","allow_larger"] = "allow_larger"`. `Needs`: `max_vehicle_length_ft: float | None = None`. `Vehicle`: `length_ft: float | None = None`.

## Global constraints

- `contracts/` is edited only by the supervisor, only after the request files below exist, and only additively (v1.2). Non-additive change: stop and ask the owner.
- Hard limits are code, never model output. Violation strings keep their current shape.
- No network in `make verify`, `make test`, `make report`. Network only in `make data`. `data/raw/` and `data/reference.db` are never committed.
- Every random draw uses a seeded `random.Random`; same seed, byte-identical output.
- The existing 24 scenarios and their baseline fleets are not edited. New realism goes into new fleet profiles and new scenarios.
- Products stay SIMULATED. Every mock response carries `"simulated": true`. No client names or documents.
- Every invented behaviour, field or number gets an `ASSUMPTIONS.md` row and a code tag. Reserved id blocks (so parallel agents never collide): Step 1 `A65-A74`, Step 2 `A75-A89`, Step 3 `A90-A104`, Step 4 `A105-A114`, data pipeline `A115-A124`.
- "Done" is pasted `make verify` output.

## Review Focus (cases no single task would otherwise pin)

1. Operator with an expired passenger endorsement or expired lift training is offered a block: must be a violation on the day it expires, not the day after.
2. Dirty id (`bus 44`, `BUS-044`, `BUS-44 `, O versus 0) must resolve to the same vehicle as `BUS-44`, never create a second vehicle.
3. Two candidates within 0.1 confidence must go to a human; a lone candidate at 0.85 must also go to a human (below the 0.9 MATCH threshold), never auto-resolve.
4. An event timestamped before a device swap resolves to the old vehicle; exactly at the swap, to the new one (`valid_from` inclusive, `valid_to` exclusive).
5. A retired asset in maintenance is never a spare, never a fault source, and a work order against it is blocked.
6. A zone-less local time in the fall-back hour (01:30 twice) and in the spring-forward gap normalises deterministically to UTC.
7. A spare reused across two non-overlapping blocks is one spare consumed; across two overlapping blocks it is a violation.

---

## Gate 1 summary (one screen)

- **Four steps, five waves, about six builders plus reviewers.** Step 1 first (everything depends on it) alongside the data pipeline (independent). Steps 2 and 3 in parallel after Step 1. Step 4 after. Final verification of all three profiles by the supervisor alone.
- **Contract v1.2 is one supervisor commit before any builder starts**, via three request files: `B-1` (crosswalk validity, resolution outcome, event identity attributes), `D-1` (operator qualification fields; hours limits), `C-1` (clearance items, plan/decision-record fields, `vehicle_fit`, length attributes).
- **Biggest risks:** (1) the spare-reuse change touches four places that must agree (workflow check, reasoning validator, adapter re-check, F invariant); (2) several dataset URLs in the manifest are unverified; (3) parallel steps colliding in `scenarios.py`, `seed.py`, `maintenance.py`.
- **Needs from you at Gate 1:** answers to the five questions at the end.

## Contract request files (exact field lists)

All additive; every new field optional with a default; `make schemas` regenerates `contracts-schema/`; `tests/contracts` stays green.

**`coordination/requests/D-1.md` (operator qualification and hours)**
- `Operator`: `licence_class: Literal["A","B","C"] | None = None`; `endorsements: list[str] = []` (e.g. `"P"` passenger); `trained_vehicle_types: list[str] = []` (e.g. `"standard"`, `"articulated"`); `training_valid_to: dict[str, datetime] = {}` (key = endorsement code or trained type or `"lift"`); `medical_valid_to: datetime | None = None`; `last_duty_end: datetime | None = None`; `duty_start_today: datetime | None = None`. Existing `qualifications` and `lift_trained` stay (legacy; ignored by the new check when the new fields are set).
- `Limits`: `max_operator_hours_week: float = 70.0` (window = 8 days, the 49 CFR 395.5 default; ASSUMED); `min_rest_hours: float = 8.0`; `max_spread_hours: float = 15.0`.

**`coordination/requests/B-1.md` (identity)**
- `CrosswalkEntry`: `valid_from: datetime | None = None` (inclusive), `valid_to: datetime | None = None` (exclusive), `vin: str | None = None`, `plate: str | None = None`, `display_name: str | None = None`.
- New models in `contracts/crosswalk.py`: `ResolutionOutcome = Literal["MATCH","AMBIGUOUS","NO_MATCH"]`; `Candidate(canonical_id: str, score: float, evidence: list[str])`; `Resolution(outcome, confidence: float, candidates: list[Candidate])`.
- `Crosswalk.lookup(..., at: datetime | None = None)` and `Crosswalk.candidates(agency, product, native_id, at=None) -> list[CrosswalkEntry]` (protocol additions; existing callers unaffected).
- Vehicle-event `data` payloads (`VehicleFaultDetected`, `DefectReported`, `VehicleStatusChanged`, `PMOverdue`, `WorkOrderOpened`, `WorkOrderClosed`): optional `native_vin`, `native_plate`, `native_name`.

**Not requested (deliberately):** no change to `Vehicle` (VIN and plate are identity evidence in events and crosswalk rows, not canonical vehicle attributes).

## Data pipeline (Track A lead; `src/transitos/data/`, `data/`, `fixtures/`, `config/`)

Task list. Each stage is a function with offline tests. Fetching into `data/raw/` is the only network step (`make data`).

- [ ] **P1 `fetch.py`**: read `data/MANIFEST.yaml`, honour `fetch: auto|manual|reference_only`, 1 req/s, the manifest user agent, write `PROVENANCE.json` per file and `data/MANIFEST.lock.json` (sha256, bytes, fetched_at, http_status, verified). Tests with a stubbed HTTP transport: throttle respected, `reference_only` never fetched, a failing link is recorded not raised.
- [ ] **P2 `verify.py`**: status 200 and content type; CSV header row and row count against `expected_rows`; XLSX sheet list and real headers; write `data/derived/<id>_columns.json` and `data/FETCH_REPORT.md`. Checklist per dataset, tick only what was observed: `ntd_rvi_2024` (headers, sheets, vehicle-type values, VIN/plate columns present or absent); `ntd_breakdowns_2022_2024` (about 3,731 rows with `$limit` set, `mode` and `mode_name` values, meaning of the `_1`/`_2` columns); `ntd_breakdowns_agency_totals`; `ntd_vehicle_type_count` (v3 export auth, else `/resource/` fallback); `ntd_vehicle_age_distribution` (guessed URL); `ecfr_565_15` and `ecfr_395_5` (HTML parses, worked example present); `cincinnati_fleet_work_orders` (probe `$limit=5`, headers, licence); vPIC reference only.
- [ ] **P3 `load.py`**: verified tables into `data/reference.db` as `ref_<id>` plus `ref_provenance`. Nothing at runtime reads it.
- [ ] **P4 `derive.py`**: `data/derived/fault_rates.yaml` (miles between major failures per mode, per year and pooled, p10/p25/p50/p75/p90, small-fleet cut `agency_voms` 10-50, sample sizes; rows with failures > 0, revenue miles > 0, no quality flag), `data/derived/fleet_mix.yaml` (share by type, make, model, seats, length, fuel, ADA; manufacture-year and model-year gap), optional `data/derived/repair_durations.yaml`. Each file has a provenance header comment.
- [ ] **P5 loaders and `make data-check`**: pydantic-validated loaders that fail loudly on a missing or malformed file; `make data-check` (offline) joins `make verify`.
- [ ] **P6 hand-written fixtures** (no download, own words, ASSUMED, ids `A115+`): `fixtures/j1939_spn_fmi.yaml` (about 15 rows), `config/fault_mix.yaml`, `config/shop_behaviour.yaml` (fallback if no usable duration data).
- [ ] **P7 `.gitignore` fix** (supervisor, Wave 0): replace `data/` with `data/raw/`, `data/reference.db`, `data/MANIFEST.lock.json` so derived files, fixtures and the manifest are committable.

If a link fails: record in `FETCH_REPORT.md`, continue, fall back to hand-set defaults tagged `source: assumption`.

## Step 1: Fix the base fleet (sequential, first; builder S1; amended)

**Files (by track):** C new `src/transitos/clearance/` (`facts.py`, `rules.py`, `evaluate.py`, `rank.py`), `tools/policy.py` (delegates), `tools/read_adapters.py` and `write_adapters.py` (capability profile, call clearance), `tools/config/licence_classes.yaml`, `tools/config/vehicle_classes.yaml` (class order, ASSUMED); D `workflow/limits.py` (thin wrapper over clearance), `workflow/graph.py` (`do_check`, facts carry provenance), `workflow/reasoning.py` (`_assign`, `validate_output` call clearance, top-three ranking), `workflow/plan.py`; A `mocks/seed.py`, `mocks/workforce.py`; F `verification/invariants.py` (tripwire only), `fake_world.py`, new `scenarios_step1.py`, golden table `tests/verification/golden_clearance.yaml`; integration `integration/world.py`, `integration/fleets.py`; E panel shows clearance items and acknowledgement.

- [ ] **1.1 Clearance core.** Interfaces: `Datum(value: Any, basis: Basis, note: str = "")`; `CapabilityProfile(product: str, fields: dict[str, Basis])` (each read adapter returns one); `evaluate_option(option: Option, facts: Facts, policy: AgencyPolicy, at: datetime) -> Clearance` with `Clearance(items: list[ClearanceItem], excluded: bool, warnings: int, unverifiable: int, tier: Literal["exact","larger"])`; `safe_evaluate(...)` wraps it and returns `excluded=True` with a `clearance_error` item on any exception (fail closed). Tests: golden table (one row per rule x status x basis, written from the policy text, not from the code), property test that missing data never yields `violation` and never yields `pass`, and a test that an injected exception excludes the option.
- [ ] **1.2 Qualification and fit rules.** Licence class, endorsement, trained vehicle type, lift/ramp training, expiry (inclusive on the day it expires), against `licence_classes.yaml`. Vehicle fit: exact is tier 1; larger class is tier 2 under `allow_larger` (`warn`, `requires_ack`); smaller is never a candidate; `max_vehicle_length_ft` violated by a larger vehicle is a violation. `exact` setting turns tier 2 into exclusion.
- [ ] **1.3 Hours, spares, availability, double-booking.** Daily and weekly caps (weekly window 8 days, ASSUMED); rest and spread are optional rules; last-spare counts distinct spares consumed (greedy interval assignment, a spare may serve several non-overlapping blocks, overlapping use is a violation); availability window; double-booking. All inside clearance; `do_check`, `validate_output` and the adapter re-check call it. `workflow/limits.py` keeps its public names as thin wrappers so existing tests stay valid.
- [ ] **1.4 Ranking.** `rank_options(options: list[tuple[Option, Clearance, float]]) -> RankedList`: excluded options dropped; order by `unverifiable`, then `warnings`, then soft score; `RankedList.top3` with `reason_first_beat_second: str`. The plan carries #1 as the steps and #2/#3 in `alternatives`; `Plan.rank_reason` holds the sentence. The decision record stores `vehicle_fit`, every non-native clearance item, and the approver's `acknowledged` list; execution refuses to start while a `requires_ack` item is unacknowledged.
- [ ] **1.5 Capability profiles.** Each read adapter reports which fields it can supply natively; derived and assumed values are tagged at the point of derivation. Workforce mock gains a switch to drop operator fields (used by the half-missing scenario).
- [ ] **1.6 Fleet.** agency-042 operators get licence class, endorsements, trained types, expiry dates, deterministic; `ARTIC` becomes a trained vehicle type. Baseline fleet in `integration/fleets.py` gets the new fields so the 24 scenarios keep their meaning (their expected outcomes do not change).
- [ ] **1.7 Verification.** `verification/invariants.py` reduced to the tripwire on final numbers; remove its own qualification and spare logic; add `test_golden_table_matches_policy_text`.
- [ ] **1.8 Tests and scenarios.** `tests/integration/test_agency_042_run.py::test_agency_042_severe_fault_completes_with_five_saga_steps`; new scenarios in `scenarios_step1.py`: `expired_training` (`ESCALATE_NO_SAFE_OPTION`), `endorsement_missing` (`ESCALATE_NO_SAFE_OPTION`), `spare_reused_across_blocks` (`PLAN_VALID`, one spare), `weekly_hours_cap` (`ESCALATE_NO_SAFE_OPTION`), `larger_class_fallback` (`PLAN_VALID` only after acknowledgement; no exact spare exists), `length_restriction_blocks_larger` (`ESCALATE_NO_SAFE_OPTION`), `half_operator_fields_missing` (`PLAN_VALID` after acknowledging the unverifiable items), `vehicle_fit_exact_excludes_larger` (`ESCALATE_NO_SAFE_OPTION`). Runner gains script op `("ack", [rule ids])`.
- [ ] **1.9 Gate:** `make verify` pasted; A26, A49, A14 resolved in `ASSUMPTIONS.md`; Known gaps updated; reviewer R1 signs off.

## Step 2: Make identifiers messy (after Step 1 and contract v1.2)

**Files:** A `src/transitos/data/identifiers.py`, `mocks/seed.py` (new fleet profile `agency-088`), mocks (VIN/plate/name in each product), `mocks/crosswalk_seed.py`; B `plumbing/resolver.py`, `plumbing/native_id.py`, `plumbing/crosswalk.py`, `plumbing/pg_stores.py`, `plumbing/crosswalk_service.py`, watch adapters (carry native VIN/plate/name into event data); E `panel/app.py` (candidates on `/unresolved`); F `scenarios_step2.py`, runner op `("pick", canonical_id)`.

- [ ] **2.1 Identifiers.** `vin_check_digit(vin17: str) -> str` and `make_vin(wmi: str, vds: str, model_year: int, plant: str, serial: int) -> str` (49 CFR 565.15 weights and transliteration; I/O/Q rejected); `make_plate(rng: random.Random, jurisdiction: str = "ZZ") -> str`; dirtying helpers `dirty_fleet_number`, `dirty_plate`. Unit test reproduces the check digit of the worked example **extracted from the fetched eCFR HTML**, not from any transcription. WMI list committed after one cached vPIC check that each WMI is unrecognised (`synthetic: true`); real WMIs only in an opt-in fixture.
- [ ] **2.2 Fleet profile `agency-088`.** Drawn from `fleet_mix.yaml`, seeded. Deliberate mess: VIN/plate in each product with inconsistent formatting; a telematics unit with no maintenance asset; a retired bus still in maintenance (asset status blocks new work orders); a non-revenue vehicle; a device swap on a known date; a cutaway with Ford chassis VIN vs body-builder make in maintenance. `crosswalk_seed.py` does not derive this profile's crosswalk from generator truth: the resolver sees only native ids, names, VIN, plate.
- [ ] **2.3 Resolver.** `class Resolver(Protocol): def resolve(self, agency: str, product: str, evidence: Evidence, at: datetime) -> Resolution`. Order: normalise; exact VIN (validate check digit; bad digit on 17 chars is a dirty-VIN signal), score 1.0; exact normalised plate 0.95; rapidfuzz fleet number inside the agency namespace 0.6-0.9; filter to rows valid at `at`. `MATCH` if top >= 0.9 and lead over runner-up >= 0.1; `AMBIGUOUS` if two within 0.1 (human queue with candidates); else `NO_MATCH` (`events.failed` as today). `CrosswalkService._handle` calls it; exact-id fast path unchanged for existing agencies.
- [ ] **2.4 Panel.** `/unresolved` returns candidates and evidence for AMBIGUOUS; a human choice via `POST /crosswalk` republishes.
- [ ] **2.5 Scenarios (all `scenarios_step2.py`):** `device_swap` (`PLAN_VALID`; second delivery of an old-unit event after the swap is parked), `ambiguous_match` (`NEEDS_HUMAN_MATCH`, zero runs and zero writes until `pick`, then `PLAN_VALID`), `renumbered_asset` (`PLAN_VALID`), `dirty_id_normalised` (`PLAN_VALID`), `orphan_telematics_unit` (`DEAD_LETTER` or `NEEDS_HUMAN_MATCH`, zero writes), `retired_asset_blocks_wo` (`ESCALATE_NO_SAFE_OPTION`). A unit test per resolver rule and per Review Focus 2-5.
- [ ] **2.6 Gate:** `make verify` pasted; ids `A75-A89` registered.

## Step 3: World engine (after Step 1 and contract v1.2; parallel with Step 2)

**Files (Track A lead, new `src/transitos/world/`):** `truth.py` (ground-truth store), `script.py` (day scripts), `lag.py`, `actors.py`, `stochastic.py`; mocks read due observations through the engine; F `verification/latency.py`, `report.py`, `scenarios_step3.py`.

- [ ] **3.1 Truth store.** `GroundTruth(clock: SimClock, script: DayScript, seed: int)` with `.advance_to(t) -> list[Beat]` (each beat exactly once) and `.state(vehicle) -> Literal["ok","faulted","out_of_service"]`. `Beat(at, vehicle, kind: Literal["fault_on","fault_off","return_to_service","dvir","callout"], params)`. Example script: bus 3 coolant fault 09:40, clears 10:15, returns 11:30.
- [ ] **3.2 Per-product lag.** `ProductLag(telematics_s=5, shop_rule_min=20, dispatcher_min=12)` (values ASSUMED); `ShopActor` and `DispatcherActor` apply truth to maintenance and scheduling after their delays; `WorldEngine.observations_log` records `(product, vehicle, kind, truth_at, visible_at)`.
- [ ] **3.3 Stochastic mode (behind a flag, second).** Failures as a Poisson process in revenue miles at rate `1 / quantile` from `fault_rates.yaml`; system and SPN from `fault_mix.yaml` and `j1939_spn_fmi.yaml`; intermittent faults that clear and recur; operator call-out 0.07 per operator-shift (sensitivity 0.10), extra board one to two of ten. Same seed, identical timeline.
- [ ] **3.4 Measurement.** `detection_latency(obs_log, audit) -> dict` with `truth_to_telematics_s`, `truth_to_first_proposal_s`, `truth_to_pull_s`, printed per scenario in the verification report.
- [ ] **3.5 Scenarios:** `scripted_day_coolant` (`PLAN_VALID`), `intermittent_fault_clears` (`MONITOR` or attach), `callout_with_extra_board` (`PLAN_VALID`); tests that scripted days are byte-identical across runs and that a fixed-seed stochastic run reproduces exactly.
- [ ] **3.6 Gate:** merge after Step 2; `make verify` pasted; ids `A90-A104` registered.

## Step 4: Two cheap behaviours (after Steps 2 and 3; one agent, sequential parts)

- [ ] **4.1 Pending Repair.** `mocks/maintenance.py`: `PendingRepair` collection, `POST /pending-repairs`, `GET /pending-repairs`; `ForemanActor(delay_min, parts_wait_prob, seed)` converts to a work order, sometimes `WAITING_PARTS`, sometimes deferring back to `PENDING`. Status vocabulary `OPEN`, `IN_PROGRESS`, `WAITING_PARTS`, `PENDING`, `COMPLETE`, WO `CLOSED`. Adapter status map: `WAITING_PARTS` to `in_progress`, `PENDING` to `open` (A23 extended). Poller must not emit `WorkOrderClosed` for `WAITING_PARTS` or `PENDING`. Delays from `repair_durations.yaml` or `shop_behaviour.yaml`. Scenarios `repair_with_parts_wait` (`PLAN_VALID`, run stays in `wait_for_repair`), `human_wo_exists_pending` (`PLAN_VALID`, no duplicate).
- [ ] **4.2 Timestamp quirks.** Maintenance and workforce CSV return zone-less local time (agency `timezone` in `tools/config/<agency>.yaml`, `zoneinfo`); telematics may emit epoch milliseconds. `tools/common.py`: `to_utc(naive: str, tz: str, *, fold: int = 0) -> datetime`; fall-back ambiguity resolved by a documented rule (first occurrence, `fold=0`; an optional `utc_offset` field disambiguates when present); spring-forward gap resolves forward by the gap; a string that carries a zone raises `ValueError`. Adapters never emit a naive timestamp (test asserts on every adapter output). Scenarios `local_time_normalised`, `dst_fallback_event`.
- [ ] **4.3 Gate:** `make verify`, then `make verify-postgres`, then `make verify-kafka`, one server at a time; ids `A105-A114`.

## Execution: waves, agents, supervision

| Wave | Who | Work | Parallel? |
|---|---|---|---|
| 0 (before agents) | Supervisor | `.gitignore` fix (P7); the three request files; apply contract v1.2 in one commit; `make schemas`; reserve assumption ids; place the phase prompt, digest, and manifest in the repo | alone |
| 1 | Builder S1 (steps 1.1-1.7). Builder DP (P1-P6) | Step 1 and the data pipeline | yes (disjoint directories) |
| Gate 2 | Reviewer R1 on Step 1; supervisor merges; `make verify` | | |
| 2 | Builder S2 (Step 2). Builder S3 (Step 3) | identity resolver; world engine | yes, separate worktrees; each in its own scenario file |
| Gate 3 | Reviewers R2, R3; merge S2 then S3; `make verify` after each | | |
| 3 | Builder S4 (Step 4) | pending repair, then timestamps (same file, sequential) | no |
| Final | Supervisor | `make verify`, `verify-postgres`, `verify-kafka` (one server at a time); refresh `ARCHITECTURE.md`, `ASSUMPTIONS.md`, `MOCK_API.md`, README counts, report | alone |

**Supervision protocol**
- Each builder works in its own git worktree, edits only its own directories and tests, and runs only the default (SQLite) profile. Only the supervisor starts Kafka or Postgres.
- A builder may launch Haiku workers only, at most 4 at a time, depth 2 (workers launch nothing). Workers get mechanical, fully specified jobs (test boilerplate from a spec, hand-written fixtures, header inspection, doc tables, grep, running a command). They never touch `contracts/`, hard-limit logic, resolver thresholds, or reviews. The builder verifies every worker output before using it.
- A builder that needs a larger model or more reasoning effort stops and sends an escalation request (what is blocked, what was tried, what is wanted). Only the supervisor upgrades.
- Acceptance checklist per report (a report missing any item is sent back): pasted command output; only own-directory files touched; assumption ids tagged and registered; seeded and deterministic; no network in tests; 24 existing scenarios green; Review Focus cases covered; number of Haiku workers used.
- Anything on hard limits, identity resolution, or timestamps also gets an independent reviewer agent plus a supervisor read of the diff.
- A cross-track blocker: write `coordination/handoffs/<track>-phase10.md` and stop.

## Risks

1. Spare reuse touches four places (graph check, reasoning validator, adapter re-check, F invariant); mitigated by one table-driven test of the same cases against all four.
2. Dataset URLs unverified; mitigated by P2 and the fallbacks.
3. File collisions; mitigated by per-step scenario files, reserved assumption id blocks, one agent for the shared maintenance mock.
4. Nested-agent permission or depth limits may differ from assumption; mitigated by a small trial before Wave 1.
5. Resolver thresholds are invented (0.9 and 0.1 from the prompt); they are ASSUMED and tested at the boundaries.
6. Public-repo hygiene: no raw data, no real alert text, no VINs that decode to real vehicles.

## Gate 1 questions: resolved

All five answered by the owner (Amendment A). Open item carried forward: the history-rewrite force-push happens after this phase. Until then every commit stays local (not pushed), and the rewrite is redone on the final history from a fresh clone; `~/transitOS_poc_rewrite_tmp` is stale.
