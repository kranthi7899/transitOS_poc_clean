# Phase 10: realistic fleet and world

Build prompt for Claude Code. Repo: `transitOS_poc`. Date written: 2026-10-11.
Companion files: `docs/RESEARCH_DATA_SOURCES.md` (what the research found) and `data/MANIFEST.yaml` (what to fetch).
**Amendment A (`docs/phases/PHASE-10-amendment-A.md`) overrides this file where they differ.**

---

## 0. How to work (read this first)

You are the program lead session for this phase. The POC already runs end to end (tracks A to F, integration, Kafka and Postgres profiles; stage 9 done, 24 of 24 scenarios). This phase makes the simulated world harder and more realistic so the orchestrator is tested on mess, not on a clean fleet.

1. **Check for overlap before anything else.** Another Claude Code session may already be writing `docs/superpowers/plans/2026-10-11-realistic-fleet-and-world.md`. Run `git status`, `git log -5 --stat`, and read that file if it exists. Treat it as a draft from another session: reconcile it with this prompt, record every difference in the plan header, and never silently overwrite it. If the two conflict on something that matters, stop and ask the owner.
2. **Plan first, then stop.** Write the plan (section 6), summarise it on one screen, and **stop at Gate 1** until the owner says go. Fetching data into the gitignored `data/raw/` is safe and may happen before Gate 1.
3. **Evidence rule (CLAUDE.md rule 6).** "Done" means `make verify` output pasted, never asserted. Report only commands you ran in this session, with real results.
4. **Follow CLAUDE.md.** Track leads edit only their own directory and its tests. Contract changes go through `coordination/requests/<track>-<n>.md` first; section 2 authorises this phase's additive changes, applied by you as program lead after the request files exist. Hard limits stay code, never model output. No client names or client documents anywhere. The products are simulated; never present them as real.
5. **Assumptions register.** Every invented behaviour, field or number gets a row in `docs/ASSUMPTIONS.md` (next free id after A64) with the real source to check and the question for Transit Technologies. Tag the code with the id.

## 1. Read first

In this order: `CLAUDE.md`, `docs/ARCHITECTURE.md` (especially "Known gaps"), `docs/CONTRACTS.md`, `docs/MOCK_API.md`, `docs/ASSUMPTIONS.md`, `src/transitos/mocks/seed.py` and `crosswalk_seed.py`, `src/transitos/contracts/{crosswalk,objects,policy}.py`, `src/transitos/tools/policy.py`, `src/transitos/workflow/limits.py`, `src/transitos/verification/scenarios.py`, then the two companion files named above.

## 2. Decisions already made (defaults; the owner can overrule at Gate 1)

- **D-A. Qualified operator** means all of: the licence class covers the vehicle class; a passenger endorsement is held; the vehicle's NTD-style type is in the operator's trained vehicle types; lift or ramp training is held when the block needs a lift. Training and medical records carry `valid_to` dates so expiry can be a scenario. ARTIC stops being a licence code and becomes a training record. The vehicle class to licence class mapping is a config table, tagged ASSUMED.
- **D-B. Hours policy** is agency configuration, not law (public agencies are exempt from FMCSA hours rules under 49 CFR 390.3T(f)(2)). Defaults from 49 CFR 395.5: 10 driving hours per shift, 15 hour on-duty spread, 8 hours rest, 70 hours in 8 days. Enforce at least the daily driving cap and the weekly cap now. Enforce spread and rest if the data model supports it without breaking contracts; otherwise log it as a gap.
- **D-C. Spares.** A spare may serve several non-overlapping blocks. The last-spare hard limit counts the distinct spare vehicles consumed, not blocks reassigned. This closes the A49 / "8 sequential blocks" gap.
- **D-D. World engine.** Scripted first and deterministic. A stochastic mode comes second, seeded, using failure rates derived from the NTD data (section 4).
- **D-E. Identity resolver.** Plain-Python rules with explicit `MATCH`, `AMBIGUOUS`, `NO_MATCH` outcomes and a confidence value, behind a `Resolver` interface so Splink could replace it later. `rapidfuzz` becomes a core dependency when step 2 lands.
- **D-F. Synthetic identifiers.** VINs are valid (correct check digit) but fictitious, using a WMI that NHTSA vPIC does not recognise, flagged `synthetic: true`. Plates use a pattern with a fictional jurisdiction code (`ZZ`). Real WMIs appear only in an opt-in "realistic decode" fixture.
- **D-G. Contract changes** in this phase are additive (v1.2): crosswalk `valid_from` / `valid_to`, a resolution outcome with confidence and candidates, and operator qualification fields. Anything non-additive stops and asks the owner.
- **D-H. Layout.** The world engine lives in a new `src/transitos/world/` owned by Track A's lead. Data pipeline code lives in `src/transitos/data/`. Dependencies for the pipeline (`pandas`, `openpyxl`, `httpx`, `pyyaml`) go in an optional extra `data` so the default profile stays zero-infra.

## 3. Rules for this phase

- **No network in `make verify`, `make test` or `make report`.** Tests read only committed files. Network access happens only in `make data`.
- **Raw data is never committed.** `data/raw/` and `data/reference.db` are gitignored. Commit only small derived files and fixtures (a few hundred rows at most).
- **Federal sources are public domain; still record provenance.** Do not scrape any site whose terms forbid it. Specifically: do not download FASTER customer help pages, Fleetio docs or Samsara docs into the repo; read them for vocabulary or shapes and write your own words. The Samsara OpenAPI repo may be cloned into `data/raw/` for reference only; hand-write pydantic models inspired by its shapes. It has no licence file, so never vendor or generate clients from it.
- **No real agency alert text in the repo.** Sample feeds only to learn style (length, cause and effect pairing), then write fictional templates with fictional routes and stops.
- **Be polite to servers:** a descriptive `User-Agent`, 1 request per second at most, caching, and no retry storms. vPIC has no published limit, so keep it to a handful of cached calls and never call it from a test.
- **If a link fails, report it; do not guess.** Record the failure in `data/FETCH_REPORT.md`, continue, and fall back to hand-set defaults tagged `source: assumption`.
- **Determinism.** Every random draw uses a seeded `random.Random`. The same seed gives byte-identical fleets and timelines.
- **Do not break the existing 24 scenarios.** Their baseline fleets stay as they are. New realism goes into new fleet profiles and new scenarios.

## 4. Data pipeline (fetch, verify, load, derive, seed)

The research found only two datasets worth downloading. Everything else is rules or invention. Pipeline stages, each a function with tests that need no network:

**4.1 Fetch.** `uv run python -m transitos.data.fetch` (also `make data`) reads `data/MANIFEST.yaml`. For each entry whose `fetch` is `auto`: GET with timeout and the throttle above; write the file under `target_path`; write `PROVENANCE.json` beside it (url, fetched_at, http_status, bytes, sha256, licence, source page). Never edit the hand-curated manifest; write results to `data/MANIFEST.lock.json` (sha256, bytes, fetched_at, http_status, verified true or false). Entries with `fetch: manual` are landing pages: read the page, find the direct link, record it in the lock file, and ask the owner before adding a new host. Entries with `fetch: reference_only` are never downloaded unattended.

**4.2 Verify before trusting.** The research did not download any of these files and several links are unconfirmed. For every dataset: confirm status 200 and content type; for CSV, print the header row and row count and compare with `expected_rows` in the manifest; for the NTD XLSX, list the sheets and the real column headers. Save the real headers to `data/derived/<id>_columns.json` and use those, not the names written in the research. Socrata returns a small page unless `$limit` is set, so always set it and check the row count (the breakdowns table should be about 3,731 rows). If the data differs from what the research said, write the difference into `data/FETCH_REPORT.md` and use what you observed.

**4.3 Load into a reference database.** Load each verified table into SQLite `data/reference.db` as `ref_<dataset id>` plus a `ref_provenance` table (one row per file). This is for exploration and for the derive step to query with SQL. Nothing at runtime reads it.

**4.4 Derive small committed artifacts** (`data/derived/`, YAML or JSON, each with a header comment: source dataset, vintage, query, row counts, date derived):
- `fault_rates.yaml` from `ntd_breakdowns`: inspect the `mode` and `mode_name` values first (bus is probably `MB`, demand response `DR`; check). For each mode, per agency and year: miles between major failures = revenue miles / major mechanical failures, keeping rows with failures > 0, revenue miles > 0 and no data-quality flag set (the `_1` and `_2` columns are probably "questionable" flags; confirm). Report sample size and the p10, p25, p50, p75 and p90 of the distribution, for 2024 and for 2022 to 2024 pooled, plus a small-fleet cut (`agency_voms` 10 to 50 if enough rows). The stochastic world engine models failures as a Poisson process in revenue miles with rate 1 divided by the chosen quantile. The "2,700 to 12,000 miles" range quoted earlier is unverified; compute it, do not cite it.
- `fleet_mix.yaml` from the NTD revenue vehicle inventory: share by vehicle type, make, model, seating capacity, length, fuel and ADA equipment for bus and cutaway types, plus the manufacture-year and model-year gap (the NTD glossary notes a model-2013 vehicle was likely manufactured in 2012; give mock vehicles both fields). The inventory describes fleets, not individual buses, and probably has no VINs or plates; confirm.
- `repair_durations.yaml` (optional): only if the Cincinnati fleet work-order dataset turns out to expose a CSV and a usable licence. Probe `https://data.cincinnati-oh.gov/resource/2a8x-bxjm.csv?$limit=5` first and record the real headers. Derive open-to-close durations by work type (lognormal shape, long parts-wait tail). It is a general city fleet, so use the shape, not the absolute numbers. If it is not usable, do not block: hand-set `config/shop_behaviour.yaml` and tag it ASSUMED.

**4.5 Seed.** Mocks and the world engine read only committed derived files and fixtures, through a loader that validates the file against a pydantic model and fails loudly if it is missing or malformed. `make data-check` (offline, part of `make verify`) checks that every derived file exists and validates.

**Hand-written fixtures** (no download; own words; ASSUMED):
- `fixtures/j1939_spn_fmi.yaml`: about 15 rows. The full SAE table is paid, and no complete open table exists. Use SPN numbers from the research (110 coolant temperature, 168 battery potential, 96 fuel level, 190 engine speed, 513 torque, 91 accelerator position, 92 engine load, 174 fuel temperature, 84 vehicle speed, 183 fuel rate, and the DTC examples SPN 1322 FMI 31 and SPN 651 FMI 7). Severity is a configurable rule, never ground truth.
- `config/fault_mix.yaml`: weights across systems. No published split exists, so this is an explicit assumption. Use the FTA major-failure examples (brakes, doors, engine cooling, steering, axles, suspension) and the common road-call categories (air conditioning, body, wheelchair lift or ramp, electrical, transmission, tyres) as the category list.
- `config/shop_behaviour.yaml` (fallback for 4.4).

## 5. The four steps

Each step lists what to build and what proves it. Do them in this order. Do not start a step until the previous step's `make verify` is green and pasted.

### Step 1. Fix the base fleet

Problem (docs/ARCHITECTURE.md "Known gaps", A26 and A49): the default agency-042 seed does not complete a run, because operator qualifications are licence codes while the hard limit expects a vehicle type, and the workflow needs a distinct spare per affected block.
- Implement D-A, D-B and D-C. Investigate how `tools/policy.py`, `workflow/limits.py` and the adapter re-check currently evaluate qualification, and make the three layers (adapter, workflow limit, mock product) agree. Also resolve A14 (vehicle type strictness: the product treats the block's type as a minimum class, the adapters and workflow require equality) by choosing one rule and documenting it.
- Operator contract: additive fields for licence class, endorsements, trained vehicle types, lift or ramp training, and expiry dates. Update `Operator.schema.json` through the export step.
- Prove it: a test that agency-042 completes `severe_fault_with_spare` through `IntegrationWorld` with approval and all five saga steps; the existing 24 scenarios still pass; new scenarios `expired_training`, `endorsement_missing`, `spare_reused_across_blocks`, `weekly_hours_cap`.

### Step 2. Make the identifiers messy

- **VIN and plate generator** in `src/transitos/data/identifiers.py`: the 49 CFR 565.15 check digit (letter values A=1 B=2 C=3 D=4 E=5 F=6 G=7 H=8 J=1 K=2 L=3 M=4 N=5 P=7 R=9 S=2 T=3 U=4 V=5 W=6 X=7 Y=8 Z=9; I, O and Q not allowed; position weights 8,7,6,5,4,3,2,10,0,9,8,7,6,5,4,3,2; sum of products mod 11; remainder 10 is `X`; the check digit is position 9), position 10 model-year code, position 11 plant, and for buses a numeric last four. Unit-test it against the worked example in the CFR text itself: fetch the eCFR page (it is in the manifest), extract the example, and assert your function reproduces its check digit. Do not trust anyone's transcription of the example, including this prompt's.
- **WMI choice:** pick candidate WMIs, check once with vPIC `GetWMIsForManufacturer` and `DecodeVinValues`, cache the responses, and commit only the final list of WMIs that vPIC does not recognise. Real WMIs (Ford incomplete-vehicle `1FD`, Blue Bird `1BA` and `1BD`, Gillig `15G`, New Flyer `5FY`, ElDorado `1N9`) come from enthusiast or manufacturer pages and are unverified; check them against vPIC before using them in the opt-in realistic fixture. One good deliberate mismatch: a cutaway built on a Ford chassis carries Ford's VIN while maintenance records the body builder as the make.
- **Per-product identifiers and deliberate mess** in a new generated fleet profile (a third agency, for example `agency-088`, drawn from `fleet_mix.yaml` and seeded): VIN and plate in each product with inconsistent formatting (`BUS-044`, `bus 44`, a dropped leading zero, O versus 0, spaces and hyphens); a telematics unit with no maintenance asset; a retired bus still present in maintenance (and the rule that an asset status blocks new work orders); a non-revenue vehicle; a device swap on a known date.
- **Contract v1.2 (additive):** crosswalk entries get `valid_from` and `valid_to`; resolution returns an outcome (`MATCH`, `AMBIGUOUS`, `NO_MATCH`), a confidence, and the candidate list. Write the request file first. `crosswalk_seed.py` must stop deriving the crosswalk from the generator's own truth for this profile: the resolver gets only what a real adapter would see (native IDs, names, VIN, plate) and must work the answer out.
- **Resolver** (`src/transitos/plumbing/resolver.py`, behind a `Resolver` protocol), in this order: normalise; exact VIN with check-digit validation (1.0; a bad check digit on a 17-character VIN is a dirty-VIN signal); exact normalised plate (0.95); fuzzy fleet number with `rapidfuzz`, restricted to the agency namespace (0.6 to 0.9); then keep only crosswalk rows valid at the event's own timestamp. Decide: top score at least 0.9 and a lead over the runner-up of at least 0.1 is `MATCH`; two candidates within 0.1 is `AMBIGUOUS` and goes to the panel's unresolved queue for a human; anything else is `NO_MATCH` and goes to `events.failed` as today.
- Prove it: scenarios `device_swap`, `ambiguous_match`, `renumbered_asset`, `dirty_id_normalised`, `orphan_telematics_unit`, `retired_asset_blocks_wo`; a unit test for each resolver rule; the panel shows candidates for an ambiguous match and a human choice republishes correctly.

### Step 3. The world engine

- **Ground truth store** (`src/transitos/world/`): the physical truth about each vehicle, operator and fault, separate from every product. Products observe it through their own lens with their own delay: telematics within seconds; maintenance only after a simulated human or rule acts; scheduling only when a simulated dispatcher acts. The truth store is also what scenarios assert against (for example, detection latency).
- **Day scripts first**, deterministic and seeded, driven by the sim clock. Example: bus 3 develops a coolant fault at 09:40, it clears at 10:15, returns at 11:30. Replace the hand-fired `/sim/fault` calls in new scenarios with world events. Existing scenarios keep working.
- **Stochastic mode second**, behind a flag: failures arrive as a Poisson process in revenue miles using `data/derived/fault_rates.yaml`, system and SPN drawn from `config/fault_mix.yaml`, intermittent faults that clear and recur, operator call-outs with probability 0.07 per operator-shift (sensitivity 0.10; sources are in the research digest and are context, not calibration), and an extra board sized at one to two of ten operators. Everything seeded; document every number as an assumption with its source.
- **Measurement:** record detection latency (truth time to the time the orchestrator's run starts) and surface it in the verification report.
- Prove it: scripted-day scenarios are byte-identical across runs; a stochastic run with a fixed seed reproduces exactly; the report prints detection latency per scenario; no new scenario depends on wall-clock time or the network.

### Step 4. Two cheap behaviours

- **Maintenance shop:** the maintenance mock gets a Pending Repair (the vocabulary used by FASTER-style and Fleetio-style systems; the mock stays generic and does not copy either product). A simulated foreman converts it to a work order after a delay, sometimes with a parts-wait status and sometimes deferring a repair back to pending. Status vocabulary: `OPEN`, `IN_PROGRESS`, `WAITING_PARTS`, `PENDING` (deferred), `COMPLETE`, then work order `CLOSED`. Delays come from `repair_durations.yaml` or `config/shop_behaviour.yaml`. This must exercise the `wait_for_repair` step properly, including `human_wo_exists` when a person opens a work order first.
- **Timestamp quirks:** maintenance and the workforce CSV return local time with no zone (agency timezone from config, via `zoneinfo`); telematics may emit epoch milliseconds. Adapters normalise to UTC RFC 3339 and tests cover a daylight-saving fall-back hour (01:30 occurs twice, so the adapter must disambiguate with an extra field or a documented rule) and a spring-forward gap. Add scenarios `local_time_normalised` and `dst_fallback_event`.
- Prove it: the extended `wait_for_repair` scenario passes; the timestamp scenarios pass; adapters never emit a naive timestamp.

## 6. The plan you must write before building (Gate 1)

`docs/superpowers/plans/2026-10-11-realistic-fleet-and-world.md` (reconcile with the other session's draft, see section 0). One screen of summary on top, then: files to touch per track; the contract request files with exact field lists; the data pipeline task list with the dataset-by-dataset verification checklist; the test and scenario list with expected outcomes; the order of waves and which subagent tracks run in parallel in worktrees (the earlier tracks A, B, C, D, F pattern works; keep Step 1 sequential because everything depends on it); risks; and the questions for the owner. Then **stop** and wait for "go".

## 7. Build order after Gate 1

1. Step 1 alone. Gate: `make verify` green and pasted.
2. Steps 2 and 3 may run in parallel in separate worktrees only after the contract v1.2 request files are applied, since they share the crosswalk and the seed. Merge, run `make verify`, paste.
3. Step 4. Merge, `make verify`, then run the Postgres profile (`make verify-postgres`) and, if a broker is available, `make verify-kafka`, so the new behaviours are proven on both profiles.
4. Final: refresh `docs/ARCHITECTURE.md` (as built, known gaps), `docs/ASSUMPTIONS.md`, `docs/MOCK_API.md` (any new endpoints or fields), and `verification-report.json`.

## 8. Reporting format at every gate

Five lines at most, then evidence: what changed; the exact commands run and their pasted output (counts of tests and scenarios); what you could not verify; assumption ids added; what you need from the owner.

## 9. Do not

- Do not download or commit raw datasets, third-party specs, customer-hosted vendor manuals or real agency alert text.
- Do not call the network from tests or from `make verify`.
- Do not edit `contracts/` outside the authorised additive v1.2 changes, and never without a request file.
- Do not let a model decide a hard limit, and do not add a real LLM call in this phase (the reasoning step stays the stub plus fallback; the provider decision is separate).
- Do not rewrite the existing 24 scenarios' baseline fleets to make new work pass.
- Do not claim a link works, a column exists or a test passed unless you saw it in this session.
