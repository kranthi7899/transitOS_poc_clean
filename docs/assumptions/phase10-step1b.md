# Phase 10 Step 1b assumptions (A125-A134)

Fix round for reviewer R1's findings on Step 1 (supervisor rulings F1-F13). Reserved block A125-A134, kept in this
file so parallel builders never collide; the supervisor merges into `docs/ASSUMPTIONS.md`. Corrections to A71, A72
and A73 are made in place in `phase10-step1.md`. All products are SIMULATED; nothing here describes a real product
or agency.

| ID | Assumption | Where | Status | Question for the real owner |
|---|---|---|---|---|
| A125 | A block occupies the half-open interval [start, end): blocks that touch end-to-start do not overlap, for double-booking, spare reuse and operator commitments alike | `clearance/rules.py::overlaps`, golden rows `touching_*` | decision | Does the scheduling product allow a vehicle or operator to start a block at the minute another ends (no turnaround time)? |
| A126 | An availability bound that the workforce product holds as empty (no unknown marker on the record) is open-ended; a BLANK cell in the export is unknown for that operator (F3) and makes the availability rule unverifiable | `clearance/rules.py::operator_items`, `tools/read_adapters.py::WorkforceReader._export` | open | Does an empty availability bound mean "available all day" or "not recorded"? |
| A127 | Minimum distinct spares (F6) is computed exactly by branch and bound, capped at 200,000 search nodes; past the cap the greedy count is used as an UPPER bound (fail closed: the pre-check may escalate, flagged `_upper_bound_unverified`). The pre-check reads `min_spares` from the live agency policy, never the start-up snapshot. When the planner's block-by-block greedy search finds no plan, it retries preferring the spares of the exact minimum cover | `clearance/rules.py::count_distinct_spares`, `clearance/rank.py::greedy_option`, `workflow/graph.py::do_check`, `verification/fake_world.py` | decision | - |
| A128 | Hours accumulation (F5): an operator the option gives at least one NEW block is judged on the hours of every affected block they end up on, kept and new alike (daily and weekly caps). An operator who only keeps blocks they already had is still judged block by block: the option does not change their workload, and whether `hours_today` already counts their scheduled blocks is open (A71) | `clearance/evaluate.py::option_items` | open | Is `hours_today` hours worked so far or hours scheduled for the day? |
| A129 | The simulated workforce export writes the token `none` for a list column (qualifications, endorsements, trained types, training dates) the product knows to be empty; an empty cell can then only mean "not recorded" and is unknown (F3). The read adapter maps `none` to an empty list | `mocks/workforce.py::LIST_COLUMNS`, `tools/read_adapters.py::_split` | open | How does the real export distinguish "holds none" from "not recorded"? |
| A130 | Block needs: a key the block does not carry, or a blank value, is NOT declared (listed in `Needs.unknown_fields`, never "no requirement"). For the two optional restrictions (`type`, `max_vehicle_length_ft`) an explicit null declares that there is none. The simulated scheduling product declares every block's length limit (null = none) | `tools/read_adapters.py::_needs`, `mocks/scheduling.py` seeder | open | Does the real block record carry each need explicitly? |
| A131 | The scheduling capability profile is computed from the vehicle payload: a field is native when every vehicle record carries the key; a null or blank value is unknown for that vehicle only. Missing placeholders (`seats` 0, `type` "bus", ...) exist only because the canonical object needs a value; the rules never read a field listed as unknown | `tools/read_adapters.py::scheduling_profile`, `map_vehicle` | decision | - |
| A132 | Revalidation (F1) re-judges the approved plan's own option on fresh facts and replaces its clearance. Before any write: an unacknowledged requires_ack rule or any new warn/unverifiable/violation item sends the run back to approval at the SAME plan version (same steps and idempotency keys) with acknowledgements reset; a known violation still goes through the existing stale/re-plan path. After writes have started only the assignments still ahead are re-judged and only their acknowledgement state is acted on (an unacknowledged item compensates and escalates as `stale_after_writes`); a new violation there is left to the write adapters' own re-check | `workflow/graph.py::revalidate`, `rejudge`, `execute_step` | decision | - |
| A133 | Fail closed per candidate (F4): an exception inside a direct clearance call excludes that candidate with a `clearance_error` item. In the workflow check and FakeWorld the item is shown in the decision record and the escalation; in the write re-checks it is a hard-limit block; in the read adapters the candidate is simply not listed (the reason is not surfaced to the plan). A crash anywhere else in the check escalates the run instead of leaving it GATHERING | `clearance/rules.py::guarded`, `workflow/graph.py::do_check`/`check`, `tools/read_adapters.py`, `tools/write_adapters.py`, `verification/fake_world.py`, `workflow/limits.py`, `tools/policy.py` | open | Should a malformed product record be reported to the product owner? |
| A134 | Medical card (F8) is a LEGAL rule: known expired = violation (expired on its expiry day, as A69), unknown or no date on record = unverifiable (acknowledgement). The verification FakeWorld and the workflow fakes now carry a valid card, as `integration/fleets.py` already did, so the 24 original scenarios keep their expectations; `half_operator_fields_missing` drops the medical column and therefore also acknowledges `operator_medical` | `clearance/rules.py::operator_items`, `verification/fake_world.py`, `workflow/fakes.py`, `verification/scenarios_step1.py` | decision | Is a medical card required for every operator at the agency? |

## Unchanged on purpose

- **F9** (owner question pending): a breach on an ASSUMED default length still excludes the vehicle (A70). Tagged
  in `clearance/rules.py::vehicle_length_item` and golden row `length_assumed_default_breach_is_assumed_violation`.

## Known gaps (not fixed in this round)

- **F11, F12**: reviewer R1's two nits, left unfixed by supervisor ruling. Their text was not given to this builder;
  the supervisor should paste R1's wording here when merging.
- A133: a candidate excluded by a clearance error inside a READ adapter is not surfaced in the plan or decision
  record (the workflow's own check surfaces errors it hits itself).
- A132: after writes have started, revalidation acts only on acknowledgement state, not on a new violation (the
  write adapters re-check hard limits before each write).
- A128: an operator who only keeps their own affected blocks is not re-audited for cumulative hours (depends on
  the open meaning of `hours_today`, A71).
