# S2 (Phase 10 Step 2): messy identifiers and the identity resolver

Branch `phase10-s2-identity`, rebased onto main ad71222 (after the S3 merge). Not pushed, not merged. No escalation needed: the canonical
`VehicleStatus` was not touched (a retired asset is a maintenance-asset status, enforced by the maintenance mock's
409 and a blocked write), and nothing under `contracts/` or `contracts-schema/` changed.

## What exists
- `data/identifiers.py` (49 CFR 565.15 check digit, VIN/plate generators, dirt helpers), `data/ecfr_vin.py`
  (extracts the regulation's tables and worked example into `fixtures/ecfr_565_15_example.json`),
  `data/vpic_wmi.py` (one-time cached vPIC check; 10 calls made) -> `fixtures/synthetic_wmis.yaml`,
  `fixtures/realistic_wmis.yaml` (opt-in).
- `plumbing/native_id.py`, `plumbing/resolver.py` (`Resolver` protocol, `RulesResolver`, `decide`),
  `plumbing/crosswalk.py` v1.2 store (validity key, evidence columns, `candidates`, `lookup(at=)`, `rows`, v1
  migration), `plumbing/crosswalk_service.py` (event-time fast path, resolver, fail closed),
  `plumbing/discovery.py` (crosswalk from product rosters), watch adapters carry `native_vin/plate/name`.
- `mocks/fleet_088.py` (agency-088), small hunks in `mocks/{seed,telematics,maintenance,scheduling,crosswalk_seed}.py`.
- Panel `/unresolved` + `/ui/unresolved` show outcome, candidates, evidence; a pick republishes.
- `verification/scenarios_step2.py` (7 scenarios), runner `("pick", canonical_id)`, `Outcome.NEEDS_HUMAN_MATCH`.
- Assumptions A75-A89 in `docs/assumptions/phase10-step2.md`.

## For the supervisor
- Postgres parity is by shared SQL (the crosswalk runs on `PgDb` through `pg.translate`); translate unit tests
  extended; the v1 -> v1.2 migration is copy-and-rename so the new key's index name never collides. NOT run on a
  Postgres server here.
- Merge hotspots with S3: `mocks/telematics.py`, `mocks/maintenance.py`, `mocks/scheduling.py`, `mocks/seed.py`
  (hunks are local: seeder skips/extra fields, two list endpoints, one 409), `verification/scenarios.py` (one enum
  member + one import/extend line), `verification/runner.py`, `verification/fake_world.py`.
- Open gap A89(1): a vehicle ready in scheduling whose maintenance asset is retired is not filtered from spares.
