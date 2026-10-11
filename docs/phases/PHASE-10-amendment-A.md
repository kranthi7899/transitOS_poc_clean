# Phase 10 amendment A (owner decisions at Gate 1)

Date: 2026-10-11. Where this differs from the build prompt or earlier replies, this file wins.
Note: the owner referred to a longer "revision 2" of this file that is not in the repo. This copy records the
decisions as stated in the Gate 1 reply; replace it with the full revision when available.

## Q1. Vehicle type is graded, not a hard wall
- Exact type is the best tier. A larger class is a fallback tier: allowed, shown as a warning, needs approver
  acknowledgement, and ranks below every exact-type plan. A smaller class is never a candidate (it fails the
  seat, wheelchair and lift rules anyway).
- If the block declares a restriction (for example maximum vehicle length), a larger vehicle that violates it is a
  hard block.
- New agency setting `vehicle_fit: exact | allow_larger` (default `allow_larger`), recorded in the decision record.
- The scheduling mock stays permissive on purpose (documented difference).
- Hard (absolute) rules stay hard only for KNOWN violations: vehicle not ready, known capacity or lift shortfall,
  known legal hours cap, known missing qualification or lift training, availability window, last-spare rule, kill
  switch, double-booking.

## Q2. One clearance module
- One pure module, `src/transitos/clearance/`, called by the planner, the write adapters, `validate_output` and
  verification. No second rule implementation.
- Verification keeps only a small outcome tripwire on the final numbers (hours cap, minimum spares left, no
  double-booking) plus a golden table written from the policy text.
- Fail closed on bugs or exceptions inside clearance.

## Q3. Missing data
- Do not assume any field exists. Each adapter reports a capability profile.
- Each rule resolves its data in order: native, derived from other data, agency default tagged ASSUMED, unknown.
- Unknown on an optional rule means `not_evaluated`. Unknown on a safety or legal rule means `unverifiable`: the
  option stays on the list, ranks below fully verified options, and needs the approver to acknowledge that rule.
- Missing data is never a violation and never a silent pass.
- Every derived, assumed, unverifiable or not-evaluated item is shown in the plan, the decision record and the
  panel.
- Add a scenario that runs a fleet with half the operator fields missing and still completes.

## Q4. NTD links
If the NTD links fail, use hand-set failure rates tagged ASSUMED, and say so wherever they appear.

## Q5. History push
The old-history rewrite and force-push happen after this phase, not before. The owner will say if that changes.

## Ranking
Find the best plan, fall back to the next best. Hard rules decide who is on the list, then rank by unverifiable
count, warnings, then soft terms. Keep the top three plans with a one-line reason why #1 beat #2.
