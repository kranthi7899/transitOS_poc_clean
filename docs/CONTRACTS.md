# Contracts (frozen v1)

Source of truth: `src/transitos/contracts/*.py`; JSON Schema in `contracts-schema/` (`make schemas`).

| File | Defines |
|---|---|
| `envelope.py` | CloudEvents 1.0 envelope, event types, topic names, start/update/resume classes |
| `events.py` | Per-type `data` payloads, publisher per type |
| `ids.py` | Deterministic event IDs, run ID, idempotency key |
| `objects.py` | Vehicle, Block, Operator, WorkOrder, Plan, Step, RunState, DecisionRecord, run states |
| `results.py` | `Result` ok/partial/unavailable |
| `tools.py` | ReadTools / WriteTools protocols, compensation map, saga order |
| `bus.py` | Producer/Consumer protocols (ordered per key, at-least-once, replay) |
| `crosswalk.py` | Crosswalk protocol |
| `policy.py` | Per-agency policy and limits |

Rules: schemas only grow; consumers ignore unknown fields; breaking change = new event type; changes only via `coordination/requests/`.

## v1.1 additions (integration, additive only; schemas regenerated)

| Change | Where | Why |
|---|---|---|
| Resolved-event shape: vehicle events carry `data["refs"]` (`VehicleRefs`, optional field on every vehicle `data` model); operator events carry `data["operator_canonical"]`; envelope extension `canonicalid` (also the events.resolved partition key). Accessors `resolved_vehicle_id(data)` / `resolved_operator_id(data)`. | `events.py`, `envelope.py` | Track B emitted `refs`, Track D read `data["vehicle"]["canonical"]` (D's A11). ONE shape now: `refs`. |
| Envelope extension `failurereason` on events.failed. | `envelope.py` | Was an untyped extra. |
| `SYSTEM_ACTOR = "transitos"` and `caused_by_us(data)`: our scheduling writes send `changed_by="transitos:<idempotency key>"`, our WOs are `opened_by="system"`. | `events.py` | D ignores update events caused by our own writes (D's A12); mocks and adapters now emit exactly this. |
| Blocked-by-policy writes: `Result.blocked(KILL_SWITCH \| HARD_LIMIT, detail)` -> `status="unavailable", product="policy", note="<reason>: <detail>"`; `.is_blocked`, `.blocked_reason`. No new status value (old consumers still read it as "did not happen"). | `results.py` | Formalises Track C's convention. Never retried by the workflow. |
| `WriteTools.reassign_block` / `assign_operator` take keyword-only `compensation: bool = False`. | `tools.py` | Saga rollback restores the original assignment without forward-only checks (kill switch still applies). |
| `ids.compensation_key(run, step, v)` = `<run>:comp:<step>:<v>`; `ids.compensated_key(run, comp_key)` inverse. | `ids.py` | Compensation writes carry their own idempotency key; audit records it plus `compensates`. |
| `ActionName` includes `cancel_work_order` (request F-1). | `objects.py` | `Step.compensation` for `create_work_order`. |

`tests/contracts/test_contracts.py::test_committed_json_schemas_match_models` fails if `contracts-schema/` is stale.

## v1.2 additions (Phase 10, additive only; requests `B-1`, `C-1`, `D-1`; schemas regenerated)

| Change | Where | Why |
|---|---|---|
| `Operator`: `licence_class`, `endorsements`, `trained_vehicle_types`, `training_valid_to`, `medical_valid_to`, `last_duty_end`, `duty_start_today` (legacy `qualifications` stays) | `objects.py` | Real qualification model (D-1); fixes A26. |
| `Limits`: `max_operator_hours_week` (70), `min_rest_hours` (8), `max_spread_hours` (15) | `policy.py` | Agency-configurable hours rules, 49 CFR 395.5 defaults (ASSUMED). |
| `AgencyPolicy.vehicle_fit` (`exact` or `allow_larger`, default `allow_larger`) | `policy.py` | Graded vehicle fit (Amendment A). |
| `Needs.max_vehicle_length_ft`, `Vehicle.length_ft` | `objects.py` | A declared block restriction can hard-block a larger vehicle. |
| `ClearanceItem` (rule, status, basis, detail, requires_ack) | `clearance.py` | One rule's verdict plus the basis of its data. Missing data is never a violation, never a silent pass. |
| `Plan.clearance`, `Plan.rank_reason`, `Plan.vehicle_fit`; `RunState.acknowledged`; `DecisionRecord.clearance`, `.acknowledged`, `.vehicle_fit` | `objects.py` | Show every non-native item and the approver's acknowledgement. |
| `CrosswalkEntry`: `valid_from` (inclusive), `valid_to` (exclusive), `vin`, `plate`, `display_name`; `Resolution`, `Candidate`; `Crosswalk.lookup(..., at=)` and `Crosswalk.candidates(...)` | `crosswalk.py` | Time-bounded identity and explicit MATCH / AMBIGUOUS / NO_MATCH (B-1). |
| Vehicle-event data: optional `native_vin`, `native_plate`, `native_name` | `events.py` | Identity evidence for the resolver. |
| `Operator.unknown_fields`, `Vehicle.unknown_fields`, `Needs.unknown_fields` (request `C-2`) | `objects.py` | Per-record "blank or absent in the source", so a missing value is `unknown`, never a default pass or violation. |
