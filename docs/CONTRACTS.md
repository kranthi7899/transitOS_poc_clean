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
