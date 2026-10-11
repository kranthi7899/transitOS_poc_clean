# Undo-gap fix (A48) - handoff

Branch: worktree-agent-a223c2cd69bc25a03

## Rule chosen
**undo-A57**: compensating `reassign_block` is a *restore of the prior assignment*. The adapter sends
`X-Restore: true` when `compensation=True`; the scheduling mock accepts it only when the target is the block's
immediately previous vehicle (`previous_vehicle`, recorded on each reassign) and then skips the
out-of-service/needs checks (the earlier assignment already satisfied them) while still refusing double-booking.
Normal assignments to an out-of-service vehicle stay refused (409), and the header never bypasses that for any
other vehicle. Why: smallest change, matches real schedulers (restoring is allowed, new assignment to an
unavailable vehicle is not), and keeps saga order, idempotency keys, hard limits and "alert last" untouched.
Alternative considered and not taken: reordering compensation (make the vehicle available before restoring the
block) - it would briefly leave a faulted vehicle available with a block on a spare, a worse safety window.

## Changes
- mocks/scheduling.py: `X-Restore` + `previous_vehicle`. tools/write_adapters.py: header on compensation.
- tests/mocks/test_scheduling.py (+2), tests/integration/test_system.py: xfail test converted to a real passing
  full-undo test (reverse order reassign_block, set_vehicle_available, cancel_work_order; block back on original vehicle; no
  vehicle left out of service; each compensation in the append-only decision audit with compensates_key;
  invariants clean); old "escalates with exact state" test removed (superseded); new regression that a normal
  reassign to an out-of-service vehicle is refused.
- verification/scenarios.py: new scenario `write_rejected_after_reassign` (assign_operator rejected after
  reassign_block) -> compensated-escalated; passes on FakeWorld and the real world.

## Results (make verify, this session)
Before (from INTEGRATION.md, not re-run by me): 313 passed, 1 xfailed, 23/23 scenarios.
After: ruff `All checks passed!`, `325 passed, 24 skipped` (skips are the Kafka-marked tests, no broker),
no xfail, `24/24 scenarios passed`.

## New assumptions
- undo-A57: restore-of-prior-assignment rule above (resolves A48; mock-side behaviour, simulated product).
- undo-A58: the mock tracks only the single previous vehicle per block; a restore after two reassigns is not covered.
- undo-A59: "available again" after undo means the mock's `ready` status (what set_vehicle_available writes); the
  seed's `in_service` and `ready` are treated as equal in the test.

## Questions / gaps
- Failed publish_rider_alert still escalates WITHOUT undo (A47, F's rider_alert_last invariant). The brief asked
  for full undo there, but that contradicts the frozen invariant; left unchanged. Owner to confirm.
- assign_operator compensation cannot run in the tested path (the assign itself failed), so "operator released"
  holds trivially; compensation of a succeeded assign_operator is covered by existing workflow tests only.
- docs/ASSUMPTIONS.md A48 still says open; program lead should mark it resolved by undo-A57 and update
  INTEGRATION.md decision 1. Not verified: real Kafka profile, Postgres.
