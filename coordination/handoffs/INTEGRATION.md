# Handoff: Integration (program lead)

## What was done
The real system now runs end to end in one process (local profile) and replaces every fake on the
verification path: Track F's previously skipped real-World tests run against it and pass.

- **Contracts v1.1** (additive; `make schemas` regenerated; test keeps `contracts-schema/` in sync; see
  docs/CONTRACTS.md): resolved events use `data["refs"]` (VehicleRefs) / `data["operator_canonical"]` +
  envelope `canonicalid` (chosen over D's `data["vehicle"]["canonical"]`), `failurereason`;
  `SYSTEM_ACTOR`/`caused_by_us`; `Result.blocked(KILL_SWITCH|HARD_LIMIT)` / `.is_blocked` (convention kept,
  no new status); keyword-only `compensation=True` on `reassign_block`/`assign_operator`;
  `ids.compensation_key`/`compensated_key`. D's `plan._comp` workaround removed (request F-1 resolved).
- **Cross-track mismatches fixed at the source** (each verified by a test against the real mocks,
  `tests/integration/test_watch_vs_mocks.py`, `test_tools_vs_mocks.py`):
  1. B emitted WorkOrderClosed for a WO cancelled by our own compensation -> poller skips `cancelled`.
  2. DVIR shape: A emits `dvir:{defect_text, reported_by_role}`, B parsed `defect:{text,...}` (400) -> B fixed.
  3. `/availability-changes` item shape `{operator, status, changed_at}` -> confirmed, test added.
  4. Inclusive `updated_since`/`since` -> confirmed deduped by deterministic event ids.
  5. D's self-inflicted filter: our scheduling writes were recorded as `changed_by: "api"` -> C sends
     `changed_by=transitos:<key>`, the mock records it; WO `opened_by=system` already matched.
  6. Workforce CSV served `05:00`, C crashed parsing it (`list_eligible_operators` always unavailable) ->
     mock renders RFC3339 on the sim day.
  7. C excluded the block's current operator from eligible operators, so D always swapped operators
     (contradicting D's A13) -> C lists the current operator when eligible.
  8. `assign_operator` read-back used scheduling's block, which the workforce write never updates (false
     `partial`) -> block reads overlay workforce `GET /assignments/{block}`.
  9. C's registry base_urls did not match `mocks.run` ports -> fixed + test.
  10. B pollers caught only `httpx.HTTPError`; Starlette's TestClient raises `httpx2` errors -> a scheduling
      timeout crashed the tick -> library-independent `UpstreamError`.
  11. C passed a per-request timeout that TestClient rejects (87 warnings) -> timeout is a client setting.
  12. Panel could not approve return-to-service (it answered "already decided" because the plan decision is
      on the run) -> `scope: return_to_service` + `RunGateway.submit_return`.
- **System bugs found by the real-World run and fixed** (D): PM urgency threshold (A7) existed in policy but
  was never applied -> `monitor` rule in code; escalations did not carry the outcome tags F classifies on
  -> `product_unavailable:*` / `kill_switch` in `RunState.violations`; compensation audit keys were
  `comp:<forward key>` instead of the real compensation idempotency key; a failed rider alert was
  compensated (violates F's rider_alert_last and would put the faulted bus back) -> escalate, no undo;
  return-to-service decision now recorded in the DecisionRecord.
- **`src/transitos/integration/`**: `system.py` (`System`, `Mocks`), `ports.py` (panel ports + audited runs
  index), `recording.py` (write log, `.reads/.writes` bundle for D, chaos hooks), `fleets.py` (scenario
  baseline fleet seeded into the real mocks), `world.py` (`make_world()` for F), `demo.py`.
  `tick()` = pollers -> relay -> crosswalk -> audit consumers -> workflow consumer -> approval timers;
  `run_until_idle()`; `restart()` closes every connection and rebuilds from the same SQLite files, then
  `recover()` re-drives runs that died between checkpoints. F's World Protocol has no restart hook, so
  `IntegrationWorld.restart()` is an extra (not added to the Protocol; FakeWorld unchanged).
- `make demo`, `make report` (F scenarios on the real world), `make verify` = schemas + lint + tests + report.
- docs/ARCHITECTURE.md (new), docs/ASSUMPTIONS.md consolidated and renumbered (mapping table inside),
  docs/MOCK_API.md v1.1 clarifications.

## Test changes made with justification (no invariant changed, nothing skipped)
- `tests/e2e/test_level2.py::test_01`: the derived test assumed every plan reassigns the operator. The
  workflow keeps the block's operator unless it fails a hard limit (D's A13; consistent after fix 7). It now
  runs on the new scenario `operator_swap_needed` (`Tweaks.assigned_operator_hours_today=7`) which needs all
  five steps; new `test_01b` asserts saga order for the four-step baseline. FakeWorld mirrors the rule;
  `tests/verification/test_invariants.py`'s fixture uses the five-step scenario.
- `tests/tools`: eligible operators now include the eligible current operator (fix 7).
- `tests/plumbing`: DVIR payload uses the mock's shape (fix 2).
- `tests/workflow::test_compensation_failure_escalates_with_exact_state` now fails `assign_operator`
  (an alert failure no longer compensates); new test for the alert path.

## Results (run in this session)
- `make verify` (schemas + ruff + pytest + report): `All checks passed!`, `313 passed, 1 xfailed`, `23/23 scenarios passed`.
- Real-world e2e: `uv run pytest tests/e2e -q -k real` -> 42 passed (was 40 skipped).
- `uv run python -m transitos.verification.report --world transitos.integration.world:make_world` -> 23/23.
- `uv run ruff check src tests` -> clean. `make demo` exits 0, invariants all hold.

## Decisions the program/client must make
1. **Undo order after a failure that follows reassign_block** (A48, strict xfail
   `test_failure_after_reassign_compensates_every_step_in_reverse`): the scheduling product refuses to put
   the block back on the out-of-service faulted vehicle, so strict reverse compensation stops and the run
   escalates with exact state. Options: dependency-aware undo (restore availability first), or keep the
   safety steps (vehicle pulled, block on spare) and only undo what is safe, or a product-side override.
2. **Failed rider alert** (A47): now escalates without undo (forced by F's invariant). Confirm.
3. **Operator qualification semantics** (A26): vehicle type vs licence codes. Until decided the mocks'
   own agency-042 fleet escalates with `no_feasible_operator`.
4. **One spare per block** (A49): a vehicle with 8 sequential blocks needs 8 spares; reuse of one spare
   across non-overlapping blocks is a workflow change.
5. Vehicle-type strictness (A14) and whether the adapter re-check should include qualifications.

## Remaining gaps
- agency-042 end to end (decisions 3-4); demo and scenarios use the agency-a baseline fleet on the real mocks.
- World.deliver publishes scenario events directly onto events.raw; the product-to-bus path (webhook,
  pollers, backfill) is covered by tests/integration and the demo.
- Operations log (decision/expiry/kill-switch ordering) is in memory; durable audit is the append-only
  DecisionStore + B's raw/resolved audit tables.
- No reconcile of `partial` writes, no rider-alert correction, no timeout on return approval, edit decision
  not exercised on the real world, single-threaded driver, no Kafka/Postgres, no auth, no real LLM.

## New assumptions
A47-A50 in docs/ASSUMPTIONS.md (rider alert failure, undo order, scenario fleet, in-memory ops log).
