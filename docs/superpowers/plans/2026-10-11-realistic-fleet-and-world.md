# Realistic fleet, messy IDs, world engine: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the base fleet complete a run, make cross-product identity genuinely hard, then drive the mocks from a scripted ground truth with per-product delays and zone-less timestamps.

**Architecture:** Four stages, strictly in order; each ends with `make verify` green and its own commit. Stage 1 fixes two hard-limit mismatches in code (licence codes, spare count per horizon). Stage 2 adds validity-dated, ambiguity-aware crosswalk entries (one additive contract change, requested via `coordination/requests/`). Stage 3 adds a seeded ground-truth store that the five mocks project with their own lag. Stage 4 adds a Pending Repair flow and local-time timestamps that adapters must normalise.

**Tech Stack:** Python 3.11, pydantic v2, FastAPI mocks, SQLite/Kafka/Postgres profiles, LangGraph, pytest, ruff, uv.

**Spec:** the four-item brief the owner pasted on 2026-10-11 (this plan's "Stage 1..4"), plus `CLAUDE.md` rules and `docs/ARCHITECTURE.md`.

## Global Constraints

- Contracts in `src/transitos/contracts/` are edited only by the program lead, and only after a request file `coordination/requests/<track>-<n>.md` exists. Changes are additive; `make schemas` regenerates `contracts-schema/` and `tests/contracts` must stay green.
- Hard limits are code, never model output (CLAUDE.md rule 3). Violation strings keep their existing shape (`operator_qualification:<op>:<type>`, `no_feasible_spare:<block>`, `last_spare:...`).
- Everything seeded and deterministic: no wall clock, no unseeded randomness. All time is `SimClock`.
- Products stay SIMULATED. No client names or documents in the repo or the docs.
- "Done" means `make verify` output pasted. Kafka and Postgres profiles are re-run (`make verify-kafka`, `make verify-postgres`) at the end of Stages 2 and 4, with only one of the two servers up at a time.
- Track leads edit only their own directory plus its tests; where one commit has to cross tracks (Stage 1 fixtures), the commit message names the tracks touched.
- Skipped on purpose (owner decision): separate DB engines per mock, separate processes per mock, full relational schemas.

## Review Focus

- A bus whose operator holds `CDL-B;PAX` is offered an articulated block: must be a violation, not a silent pass.
- An event whose native id arrives dirty (`bus 44`, `BUS-044`, trailing space, lowercase) must resolve to the same vehicle as `BUS-44`, never create a second vehicle.
- Two crosswalk rows that both match an event's id and timestamp must park the event for a human, never pick one.
- An event timestamped before a device swap must resolve to the old vehicle; after the swap, to the new one; exactly on the boundary, to the new one (`valid_from` inclusive, `valid_to` exclusive).
- A retired asset still present in maintenance must never be proposed as a spare or reported as a fault source.
- A zone-less local timestamp around the DST change must normalise to one unambiguous UTC instant, and a skipped/ambiguous local hour must be handled deterministically.

---

## Stage 1: Fix the base fleet

Decision recorded (owner's recommendation, adopted): "qualified" = the operator holds every licence code the block's vehicle class requires. Licence codes and vehicle class are separate; the mapping from class to required codes is a hard-limit constant. This resolves A26. A49 (one spare per block vs shared) stays open; Stage 1 makes agency-042 complete by planning a 4 h horizon instead (blocks beyond it are re-planned when they come into the window).

### Task 1: Licence-code qualification (hard limit + invariant + fixtures)

**Files:**
- Modify: `src/transitos/workflow/limits.py` (Track D)
- Modify: `src/transitos/verification/invariants.py:75` and `src/transitos/verification/fake_world.py:73,143` (Track F)
- Modify: `src/transitos/integration/fleets.py` (`quals = ...` line) (integration)
- Test: `tests/workflow/test_limits_licences.py` (create), existing `tests/verification/test_invariants.py`

**Interfaces:**
- Produces: `REQUIRED_LICENCES: dict[str, frozenset[str]]` and `required_licences(vehicle_type: str | None) -> frozenset[str]` in `workflow/limits.py`; `HardLimits.operator_violations` unchanged in signature and violation string.
- Mapping (ASSUMED, A26): `cutaway -> {PAX}`, `standard -> {CDL-B, PAX}`, `articulated -> {CDL-B, PAX, ARTIC}`. Unknown type -> empty set (no requirement), as `needs.type is None` already does.

- [ ] **Step 1: Write the failing tests** in `tests/workflow/test_limits_licences.py`: `test_standard_block_needs_cdl_b_and_pax` (operator `["CDL-B","PAX"]` -> no `operator_qualification` violation), `test_articulated_block_rejects_operator_without_artic` (operator `["CDL-B","PAX"]`, block type `articulated` -> `["operator_qualification:op-1:articulated"]`), `test_vehicle_type_string_is_not_a_qualification` (operator `["standard"]`, block `standard` -> violation), `test_no_type_means_no_requirement`.
- [ ] **Step 2: Run** `uv run pytest tests/workflow/test_limits_licences.py -q`; expect 3 failures (the first passes only by accident of old code: confirm which).
- [ ] **Step 3: Implement** `required_licences` and swap the check in `operator_violations` to `missing = required_licences(needs.type) - set(op.qualifications)`; emit the existing string when `missing` is non-empty.
- [ ] **Step 4: Mirror the rule independently in Track F**: `invariants.py` gets its own copy of the mapping (the invariant must not import the code it audits) and a test `test_licence_map_matches_hard_limits` asserting the two dicts are equal. `fake_world.py` qualifications become `["CDL-B","PAX"]` / `["van"]` and its eligibility test uses the same subset rule.
- [ ] **Step 5: Fix the baseline fleet** in `integration/fleets.py`: `quals = "CDL-B;PAX" if t.operators_qualified else "PAX"` (unqualified = missing `CDL-B`, so `standard` blocks fail).
- [ ] **Step 6: Run** `make verify`; expect 24/24 scenarios incl. `unqualified_operator`, tests green.
- [ ] **Step 7: Commit** `fix(limits): qualification = licence codes per vehicle class (A26)`.

### Task 2: agency-042 completes a run

**Files:**
- Modify: `src/transitos/integration/system.py` (`System.__init__`, the `Deps(...)` call near line 193): accept `horizon_hours: int = 24` and pass it to `Deps`.
- Modify: `src/transitos/integration/demo.py`: pass `horizon_hours=4` where the agency-042 `System` is built (the demo's baseline fleet is unaffected).
- Test: `tests/integration/test_agency_042_run.py` (create)

**Interfaces:**
- Produces: `System(agency, mocks, data_dir=None, *, horizon_hours: int = 24, ...)`.
- Consumes: `build_fleet("agency-042", now)` (8 two-hour blocks per line vehicle from 05:00; 3 spares; 10 operators with `CDL-B;PAX[;ARTIC]`).

- [ ] **Step 1: Write the failing test** `test_agency_042_severe_fault_completes_a_run`: seed agency-042, deliver a `VehicleFaultDetected` for `T-77`, approve the plan, advance 48 h, close the work order, approve return to service; assert final status `DONE`, no `last_spare` or `no_feasible_*` violation, and `len(plan reassign steps) <= 2`.
- [ ] **Step 2: Run it;** expect failure with `last_spare:3_ready_for_8_blocks` (confirms the diagnosis) or a qualification violation if Task 1 is not merged.
- [ ] **Step 3: Implement** the `horizon_hours` parameter and use `4` for agency-042 in the test and the demo.
- [ ] **Step 4: Run** the test; expect PASS. Then `make verify`.
- [ ] **Step 5: Docs:** `docs/ASSUMPTIONS.md`: A26 -> resolved (licence codes + class map), A49 -> stays open with the note "agency-042 plans a 4 h horizon"; `docs/ARCHITECTURE.md` Known gaps: delete the agency-042 bullet.
- [ ] **Step 6: Commit** `fix(integration): agency-042 completes a run (4h horizon); A26 resolved`.

---

## Stage 2: Messy IDs

Contract request first (program lead writes `coordination/requests/A-1.md`, then edits `contracts/`).

### Task 3: Contract request and additive contract change

**Files:**
- Create: `coordination/requests/A-1.md`
- Modify: `src/transitos/contracts/crosswalk.py`, `src/transitos/contracts/envelope.py` (only if a new event field is needed; prefer none), `docs/CONTRACTS.md` (v1.2 table), `contracts-schema/*` via `make schemas`
- Test: `tests/contracts/test_contracts.py` (existing schema-staleness test), `tests/contracts/test_crosswalk_contract.py` (create)

**Interfaces (Produces):**
- `CrosswalkEntry` gains optional `valid_from: datetime | None = None`, `valid_to: datetime | None = None` (inclusive start, exclusive end), `vin: str | None = None`, `plate: str | None = None`.
- `Crosswalk.lookup(agency, product, native_id, at: datetime | None = None) -> str | None` (new optional `at`; `None` = ignore validity, today's behaviour).
- `Crosswalk.candidates(agency, product, native_id, at: datetime | None = None) -> list[str]` (distinct canonical ids, sorted; 0 = unknown, 1 = resolved, 2+ = ambiguous).
- `Crosswalk.add_ambiguous(agency, product, native_id, event_id, candidates: list[str]) -> None`.

- [ ] **Step 1: Write `A-1.md`**: problem (identity is too easy), the four additive changes above, what each track does (A seeds VIN/plate and scenarios, B implements, F adds scenarios), no breaking change (all new params optional).
- [ ] **Step 2: Write the failing contract test** `test_crosswalk_entry_validity_defaults_open` and `test_protocol_has_candidates`; run, expect failure.
- [ ] **Step 3: Edit the contract**, run `make schemas`, run `uv run pytest tests/contracts -q`; expect green.
- [ ] **Step 4: Commit** `contracts: v1.2 crosswalk validity, VIN/plate, candidates (request A-1)`.

### Task 4: Crosswalk implementation with validity, normalisation and ambiguity (Track B)

**Files:**
- Modify: `src/transitos/plumbing/crosswalk.py` (schema: add `valid_from`, `valid_to`, `vin`, `plate` columns; primary key becomes `(agency, product, native_id, valid_from)` with `valid_from` stored as `''` when open), `src/transitos/plumbing/crosswalk_service.py` (`_handle`), `src/transitos/plumbing/pg_stores.py` (same schema on Postgres)
- Create: `src/transitos/plumbing/native_id.py`
- Test: `tests/plumbing/test_crosswalk_validity.py`, `tests/plumbing/test_native_id.py`, storage contract suite parametrised over SQLite and Postgres (existing `tests/plumbing` storage contract)

**Interfaces:**
- Produces: `normalize_native(product: str, raw: str) -> str` in `native_id.py`: strip, upper-case, collapse separators, drop leading zeros in the numeric part, so `"bus 44"`, `"BUS-044"`, `" Bus-44 "` all become `"BUS-44"`; products with numeric-only ids (maintenance) strip whitespace/zeros only.
- `SqliteCrosswalk.lookup`/`candidates` try the exact id, then `normalize_native`; both filter by `valid_from <= at < valid_to` when `at` is given.
- Event `time` is the `at` passed by `CrosswalkService._handle`.
- Ambiguous outcome: event parked in a new table `xw_ambiguous(agency, product, native_id, event_id, candidates TEXT)`, `events.failed` published with `failurereason="ambiguous <entity> <product>:<native>: veh-1|veh-9"`, `xw_processed.status='parked'`. `apply_mapping(entry)` (human pick) republishes as today.

- [ ] **Step 1: Failing tests** (names and assertions): `test_dirty_ids_normalise_to_one_vehicle` (`"bus 44"`, `"BUS-044"` -> `veh-1`); `test_device_swap_resolves_by_event_time` (T-77 -> veh-1 until 2026-10-10T12:00Z, T-90 -> veh-1 from then; T-77 reused by veh-9 from 12:00; an event for T-77 at 11:59:59Z -> veh-1, at 12:00:00Z -> veh-9); `test_two_matches_are_ambiguous` (two open rows for one native id -> `candidates == ["veh-1","veh-9"]`, service parks and emits `events.failed` with the candidate list); `test_human_pick_republishes`.
- [ ] **Step 2: Run;** expect failures for each.
- [ ] **Step 3: Implement** in the order: `native_id.py`, SQLite schema + `candidates`/`lookup`, `_handle` branches (0/1/2+), Postgres schema parity.
- [ ] **Step 4: Run** `uv run pytest tests/plumbing -q`, then `make verify`.
- [ ] **Step 5: Commit** `plumbing: validity-dated crosswalk, dirty-id normalisation, ambiguous parking`.

### Task 5: Messy mock data (Track A)

**Files:**
- Modify: `src/transitos/mocks/seed.py` (vehicles get `vin`, `plate`; add `AGENCIES["agency-042"]` extras below), `mocks/scheduling.py`, `mocks/telematics.py`, `mocks/maintenance.py` (return `vin`/`plate`), `mocks/crosswalk_seed.py` (emit `vin`/`plate`, validity)
- Test: `tests/mocks/test_messy_ids.py` (create), existing `tests/mocks/test_seed.py`

**Interfaces:**
- Deliberate mismatches in the agency-042 seed (all documented in the module docstring): (1) telematics unit `T-90` with no maintenance asset (new bus `veh-13`, VIN known only to telematics); (2) maintenance asset `1999` for a retired bus: `status="retired"`, no scheduling vehicle, no crosswalk row; (3) scheduling emits `BUS-044` for `veh-4` while the crosswalk row is `BUS-44` (a dirty id emitted by the product itself); (4) veh-1's telematics unit `T-77` is swapped to `T-120` on day 2 (two crosswalk rows with `valid_to` / `valid_from` at the swap time).
- VINs: 17 characters, deterministic (`1FD` + zero-padded index), plates `TR-{idx:04d}`; two vehicles in different products carry the same VIN but different native ids (the matching evidence for tests).

- [ ] **Step 1: Failing tests:** `test_every_vehicle_has_vin_and_plate_in_each_product`, `test_retired_asset_present_in_maintenance_only`, `test_telematics_unit_without_asset`, `test_scheduling_emits_dirty_id`. Run; expect failures.
- [ ] **Step 2: Implement** the seed additions and expose `vin`/`plate` on the three products' read endpoints; update `docs/MOCK_API.md`.
- [ ] **Step 3: Run** `uv run pytest tests/mocks -q` then `make verify`.
- [ ] **Step 4: Commit** `mocks: VIN/plate, deliberate ID mismatches, validity rows`.

### Task 6: Three scenarios (Track F)

**Files:**
- Modify: `src/transitos/verification/scenarios.py` (new `Outcome.NEEDS_HUMAN_MATCH = "needs-human-match"`; `Tweaks.crosswalk: str = "clean"`; three scenarios), `src/transitos/verification/invariants.py` (ambiguity invariant: no run exists and zero writes for an ambiguous event), `src/transitos/verification/fake_world.py`, `src/transitos/integration/world.py` (apply the crosswalk variant)
- Test: existing `tests/verification/*`, `tests/e2e/*` (scenario list parametrised)

**Interfaces:**
- `device_swap`: the vehicle's telematics unit changes; the fault arrives from the new unit after the swap time -> `PLAN_VALID`; a second case in the same scenario delivers an old-unit event dated after the swap -> parked (asserts `dead_letters` contains it).
- `ambiguous_match`: two assets match the event id -> `NEEDS_HUMAN_MATCH`, zero runs, zero writes, and after `("pick", "veh-1")` in the script the run starts and completes (`PLAN_VALID` path) -> script op `("pick", canonical_id)` added to the runner.
- `renumbered_asset`: maintenance asset `1106` renumbered to `2106` with validity; a `PMOverdue` for `2106` -> `PLAN_VALID`.

- [ ] **Step 1: Add the scenarios and runner op;** run `make report`; expect the three new ones to fail.
- [ ] **Step 2: Wire** `Tweaks.crosswalk` in `IntegrationWorld.apply` and `FakeWorld.apply`.
- [ ] **Step 3: Run** `make verify`; expect 27/27 scenarios. Update README and ARCHITECTURE counts.
- [ ] **Step 4: Run** `make verify-kafka` and `make verify-postgres` (one server at a time).
- [ ] **Step 5: Commit** `verification: device_swap, ambiguous_match, renumbered_asset scenarios`.

---

## Stage 3: World engine, scripted first

### Task 7: Ground truth and day scripts (Track A, new package `src/transitos/mocks/world/`)

**Files:**
- Create: `src/transitos/mocks/world/truth.py`, `script.py`, `__init__.py`
- Test: `tests/mocks/world/test_truth.py`

**Interfaces:**
- `Beat(at: datetime, vehicle: str, kind: Literal["fault_on","fault_off","return_to_service","dvir"], params: dict)` (frozen dataclass).
- `DayScript(agency: str, beats: tuple[Beat, ...])`; `DayScript.example_coolant_fault(day: date) -> DayScript` = bus 3 `fault_on` 09:40 (SPN 110, FMI 0), `fault_off` 10:15, `return_to_service` 11:30.
- `GroundTruth(clock: SimClock, script: DayScript, seed: int)`: `.advance_to(t: datetime) -> list[Beat]` (beats now due, in order, each exactly once), `.state(vehicle: str) -> Literal["ok","faulted","out_of_service"]`.

- [ ] **Steps:** failing tests for ordering, exactly-once delivery across repeated `advance_to`, and seed determinism; implement; `uv run pytest tests/mocks/world -q`; commit `mocks/world: ground truth and day scripts`.

### Task 8: Per-product lag (Track A)

**Files:**
- Create: `src/transitos/mocks/world/lag.py`, `actors.py`
- Modify: `src/transitos/mocks/telematics.py`, `maintenance.py`, `scheduling.py` (each reads due observations from the engine)
- Test: `tests/mocks/world/test_lag.py`

**Interfaces:**
- `ProductLag(telematics_s: int = 5, shop_rule_min: int = 20, dispatcher_min: int = 12)`; `WorldEngine(truth: GroundTruth, lag: ProductLag)` with `.tick() -> None` that, on each beat, schedules: telematics fault at `at + 5 s`; `ShopActor` creates a maintenance record `shop_rule_min` later; `DispatcherActor` flips scheduling status `dispatcher_min` later (and only when the vehicle is not already out of service in scheduling).
- `WorldEngine.observations_log: list[Observation(product, vehicle, kind, truth_at, visible_at)]` (feeds Stage 3's latency measure).

- [ ] **Steps:** failing tests (telematics sees the fault 5 s after truth; maintenance after 20 min; scheduling after 12 min and never before the maintenance record when the script says so); implement; wire `/sim/world/tick`; commit.

### Task 9: Detection-latency metric (Track F)

**Files:**
- Create: `src/transitos/verification/latency.py`; Modify: `verification/report.py` (adds `latency` block to the JSON report)
- Test: `tests/verification/test_latency.py`

**Interfaces:**
- `detection_latency(obs_log: list[Observation], audit: list[AuditEvent]) -> dict[str, float]` with keys `truth_to_telematics_s`, `truth_to_first_proposal_s`, `truth_to_pull_s` (seconds, sim time).
- [ ] **Steps:** failing test on the example script (proposal is produced within one poll tick of the telematics event); implement; run `make verify`; commit `verification: detection latency`. Randomness (failure benchmarks 2,700–12,000 miles) is explicitly deferred until after this stage.

---

## Stage 4: Two behaviours from the research

### Task 10: Pending Repair converted by a simulated foreman

**Files:**
- Modify: `src/transitos/mocks/maintenance.py` (new `pending_repair` collection, `POST /pending-repairs`, `GET /pending-repairs`, WO status `parts_wait`), `src/transitos/mocks/world/actors.py` (`ForemanActor`), `src/transitos/plumbing/watch/` maintenance poller (parts-wait is not "closed": no `WorkOrderClosed`), `src/transitos/tools/read_adapters.py` (status map: `parts_wait -> in_progress`, A23)
- Test: `tests/mocks/test_pending_repair.py`, `tests/integration/test_parts_wait.py`

**Interfaces:**
- `ForemanActor(delay_min: int, parts_wait_prob: float, seed: int)`: converts a pending repair to a WO after the delay; with seeded probability puts it in `parts_wait` for `parts_wait_hours` before `in_progress`.
- TransitOS `create_work_order` becomes "create pending repair if the product only exposes that" behind a registry flag `maintenance.mode: pending_repair`; the adapter's read-back accepts the converted WO by idempotency key.

- [ ] **Steps:** failing tests (pending repair converted after delay; `parts_wait` does not resume the run; closing resumes it); implement; `make verify`; commit.

### Task 11: Zone-less local timestamps

**Files:**
- Modify: `src/transitos/mocks/maintenance.py` and `mocks/workforce.py` (render naive local time using an agency zone), `src/transitos/mocks/seed.py` (`AgencyCfg.tz`), `src/transitos/tools/common.py` (create `to_utc(naive: str, tz: str) -> datetime`), `tools/read_adapters.py` (use it for work orders, PM, and the operator CSV), `tools/config/agency-042.yaml` (`timezone: America/Los_Angeles`)
- Test: `tests/tools/test_local_time.py`

**Interfaces:**
- `to_utc(naive: str, tz: str) -> datetime`: parses `YYYY-MM-DDTHH:MM:SS` or `HH:MM` on a given date; ambiguous fall-back hour resolves to the first occurrence (`fold=0`); a nonexistent spring-forward time resolves forward by the gap; raises `ValueError` on strings that carry a zone (mixed formats are a bug).
- [ ] **Steps:** failing tests including the two DST edge cases (Review Focus); implement; run `make verify`, `make verify-kafka`, `make verify-postgres`; commit; update README numbers and `docs/ASSUMPTIONS.md` (new assumptions: pending-repair semantics, local-time rendering).

---

## Not in this plan

LLM provider wiring (after Stage 3, owner decision), random fault generation calibrated to failure benchmarks (after Stage 3), Docker compose profile, separate processes per mock.
