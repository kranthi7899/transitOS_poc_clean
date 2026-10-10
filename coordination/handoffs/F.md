# Track F handoff: verification harness

## Built
- `src/transitos/verification/`
  - `model.py`: `Observation` (runs, `WriteRecord` log, `AuditEvent` log, final vehicles/blocks/operators/WOs, alerts, dead letters, attached events, policy). One global `seq` orders writes, approvals, kill-switch toggles and audit events.
  - `scenarios.py`: 22 scenarios as data (`SCENARIOS`, `BY_ID`): `Scenario(id, events, script, tweaks, chaos, expected: Outcome, expect_*)`. Covers all 13 brief topics plus lift mismatch, lift-untrained operator, minor PM (monitor), reject, expiry, write rejected mid-plan, kill switch mid-run, duplicate write (retry), spare taken after proposal.
  - `invariants.py`: pure checkers `hard_limits`, `idempotent_writes`, `rider_alert_last`, `audit_complete`, `approval_before_write`, `no_write_after_kill_switch`; `INVARIANTS`, `check_all(obs)`, `check_by_invariant(obs)`.
  - `world.py`: `World` Protocol. `runner.py`: `drive`, `classify`, `run_scenario`. `report.py`: table + JSON. `fake_world.py`: `FakeWorld`.
- `tests/e2e/`: `world.py` (re-exports Protocol, real-world discovery), `conftest.py` (`make_world` fixture params fake/real), `test_level2.py`.
- `tests/verification/`: `test_invariants.py` (pass + violating case for every invariant), `test_report.py`.
- `coordination/requests/F-1.md`: contract bug (see below).

## Run
- `uv run pytest -q`
- `uv run python -m transitos.verification.report [--world pkg.mod:factory] [--out verification-report.json]` (exit 1 on any failure). Default world = FakeWorld.

## What the integration lead must provide
A zero-arg factory returning an object satisfying `transitos.verification.world.World`:
`apply(scenario)`, `deliver(CloudEvent)`, `approve(run_id, scope, by)`, `reject`, `advance_clock(hours)`, `set_kill_switch(on)`, `close_work_order(run_id)`, `take_spare(run_id)`, `observe() -> Observation`.
Expose it as `transitos.integration.world:make_world` or env `TRANSITOS_WORLD=module:factory`; the 40 currently-skipped "real" tests then run automatically. The world must interpret `Scenario.tweaks` (see `Tweaks` and the baseline described in the `scenarios.py` docstring) and `chaos` keys (`scheduling: timeout`, `maintenance: duplicate`, `reject_write: <action>`, `kill_after_writes: N`), and fill `Observation.writes/audit` with a shared monotonically increasing `seq`. Compensation writes need `compensation=True, compensates_key=<original key>`; approvals are `AuditEvent(kind="approval", data={"scope": "plan"|"return_to_service"}, plan_version=...)`; each write needs an `AuditEvent(kind="write", key=<same key>)`.

## Test result
```
79 passed, 40 skipped in 0.12s     (skips = "real World not provided yet" parametrisations)
ruff check src/transitos/verification tests/e2e tests/verification -> All checks passed
report on FakeWorld: 22/22 scenarios passed
```

## Assumptions (new)
- A11: Run outcome classification uses contract fields only. `RunState.violations` strings: prefix `kill_switch`, prefix `product_unavailable`, anything else = hard limit blocked (e.g. `last_spare`, `no_feasible_spare`, `wheelchair_mismatch`, `lift_mismatch`, `operator_hours`, `operator_unqualified`). Rejected = status `CLOSED_REJECTED`; expired = `decision=="expired"`; monitor = `plan.decision=="monitor"`; dead letter = no run + `Observation.dead_letters`. Track D must follow this or tell me.
- A12: Operator qualification check = `block.needs.type in operator.qualifications`; lift blocks need `lift_trained`. Spare check: count of `ready` vehicles with no block, excluding the faulty vehicle, must be >= `min_spares` after a surviving reassign.
- A13: The "12 acceptance tests from the spec": the spec is not in the repo, so I derived 12 from CLAUDE.md rules and the brief (happy path/saga order, no write before approval, spare limits, accessibility, operator limits, duplicates, active-run attach, human WO reuse, compensation in reverse, kill switch, reject/timeout, unavailable/unresolved input never guessed). Plus one parametrised test over all 22 scenarios. Please map to the real spec if it differs.
- A14: Return-to-service (`set_vehicle_available`) is a separately-approved phase after the rider alert, so it is exempt from "alert last"; it needs an approval with scope `return_to_service`. Compensations are exempt from approval but NOT from the kill switch (strict).
- A15: `autonomy=recommend` for an action means any write of it is a violation; `act` exempts approval.

## Contract issue
`coordination/requests/F-1.md`: `Step.compensation` (typed `ActionName`) cannot hold `"cancel_work_order"`, which `COMPENSATION["create_work_order"]` returns, so building such a Step fails validation. FakeWorld works around it by leaving `compensation=None` on that step. D/C will hit this too.

## Not done / honest gaps
- FakeWorld is my own model of intended behaviour; it validates the harness and invariants, not the real system. All 40 "real" tests are skipped until the integration World exists.
- Scenarios whose behaviour depends on real wiring (replayed event stream, process kill/restart mid-run, late/out-of-order events, 429 backoff) are not covered; the World interface has no restart hook yet.
- `hard_limits` inspects final state, so operator `hours_today` must be the pre-run value in `Observation.operators`.
- Report has no `make verify` hook (Makefile not mine); suggest `uv run python -m transitos.verification.report` be added to `make verify`.
