# Handoff: Track D (Workflow brain)

## Built (src/transitos/workflow/)
- `graph.py`: LangGraph with SqliteSaver. Nodes: gather (parallel reads) -> check (hard limits) -> reason (validate, 1 retry, fallback) -> policy -> propose -> gate -> {await_approval -> route -> revalidate} / execute_step (one write per pass) -> compensate (one undo per pass, reverse) -> escalate; wait_for_repair -> route_repair -> return_gate -> return_to_service -> close; close_rejected. `gate` is a non-interrupt node doing status/deadline bookkeeping so the 3 interrupt-only nodes (await_approval, wait_for_repair, return_gate) do no writes and touch no store.
- `reasoning.py`: `Reasoner` protocol, `StubReasoner`, `code_ranked_fallback`, `validate_output` (schema, spare/operator in feasible lists, pull_at in window, distinct picks, last-spare rule).
- `limits.py` (`HardLimits`, injected), `plan.py` (code builds steps from validated output), `store.py` (`RunsIndex`: sqlite event-dedupe, one active run per vehicle via unique index, plans, decision records, escalations, stale/pending-resume flags), `starter.py`, `timers.py`, `fakes.py`.

## How to run
`uv run pytest -q tests/workflow` (31 total tests in repo pass; `uv run ruff check src/transitos/workflow tests/workflow` clean).

```
31 passed in 0.64s
```

## Public entry points
- `graph.build_graph(deps: Deps, checkpointer) -> CompiledGraph`; `graph.Deps(tools, policy: Callable[[], AgencyPolicy], limits: HardLimits, store: RunsIndex, clock: Callable[[], datetime], reasoner=StubReasoner(), ...)`; `graph.make_checkpointer(path)`; `graph.run_config(run_id)` (thread_id = run_id, recursion_limit 500; ALWAYS use it, default 25 is too low).
- `tools` is ToolSet-like: uses `.reads`/`.writes` if present, else the object itself must implement both protocols.
- `starter.handle_event(event, runs_index, graph, clock=None) -> HandleResult(action, run_id)`; `starter.resume_approval(graph, runs_index, run_id, decision, by, edits=None, at=None)` (decision approve|reject|edit; edits = `{"assignments":[{"block_id","spare_id","operator_id"}]}`); `starter.resume_return(graph, run_id, decision, by)`.
- `timers.expire_approvals(graph, runs_index, clock) -> list[run_id]` (call after advancing the sim clock).
- `store.RunsIndex(path)`: for the panel: `list_runs()`, `get_run`, `get_plan`, `get_decision(run_id, plan_version)`, `get_escalation`, `context`. Full state: `graph.get_state(run_config(id)).values["run"]` (RunState dump).
- `fakes.py`: `FakeWorld/FakeTools/FakeClock/ScriptedReasoner`, `fault_event`, `wo_closed_event`, fault-injection flags.

## A4 finding (verified by test_a4_interrupt.py on langgraph 1.2.14)
CONFIRMED: on resume the interrupted node re-runs from its first line (code before `interrupt()` executes twice); earlier nodes do not re-run. Hence interrupt nodes are pure.

## Behaviour decisions / new assumptions
- A11: resolved events carry canonical vehicle id at `data["vehicle"]["canonical"]` (VehicleRefs); fallbacks `canonical_vehicle_id`/`vehicle_id`. Track B must match.
- A12: update events caused by our own writes (`opened_by=="system"` or `changed_by` starting `transitos`) are ignored, else they would invalidate our plan.
- A13: assign_operator step is emitted only if the block's current operator is missing or fails hard limits; operator limits = hours_today+block hours <= max, qualification contains `needs.type`, lift training, availability window.
- A14: hard-limit violation in check (no feasible spare/operator, last-spare for all window blocks) escalates before the model is called; read result not `ok` (incl. partial) -> escalate, never guess.
- A15: kill switch: checked live before every write and every compensation; on trip, halt and escalate with exact state, NO auto-compensation (compensation is a write). With kill switch on at start the plan is persisted as recommendation then escalated.
- A16: per-step autonomy: `act` steps run before approval; the first `approve` step triggers the (single) approval; "reject mid-plan" = reject after act-steps executed -> reverse compensation then escalate. Stale/expired/edit after writes -> compensate then escalate (no re-plan).
- A17: stale before any write -> plan_version+1 (fresh idempotency keys), re-reason, new approval. Max 3 replans.
- A18: write retries: 3 attempts with same key in-node; `partial` results (write sent, read-back failed) are NOT retried and are recorded as done, listed in ctx `unverified` (reconcile is not implemented); Result with product "policy" not retried.
- A19: escalated/rejected/done runs release the vehicle's active slot (a new run can start even if escalation left inconsistent state).
- A20: WorkOrderClosed arriving before the run reaches wait_for_repair is parked and applied at the gate. Return-to-service uses policy autonomy `return_to_service` (approve -> second interrupt; no timeout on it).
- Step names in idempotency keys: `<action>` or `<action>:<block_id>`; compensation keys `<run>:comp:<name>:<version>`.

## Contract notes
- `Step.compensation` for create_work_order ("cancel_work_order") failed validation on the frozen ActionName; `plan._comp` falls back to None if validation fails (compensation is decided from `tools.COMPENSATION` by action anyway). Program lead says main now allows it; the workaround can be removed, it auto-uses the value when valid.
- No pyproject changes needed.

## Needs from others
- Track C: ToolSet shape (`.reads`/`.writes`); write calls must be idempotent by key; unavailable-by-policy results `product="policy"`.
- Track B: wire `events.resolved` consumer to `handle_event` (commit offset after it returns); panel (E) calls `resume_approval`/`resume_return`; something must call `expire_approvals` periodically.

## NOT done / gaps
- No reconcile for partial writes; no correction rider-alert compensation; no concurrency control (single-threaded invocation per run assumed; two simultaneous resumes of one run not guarded); approval-timeout not applied to return approval; a real LLM reasoner is not implemented (interface only); `get_vehicle_history` unused; multi-block pull_at_block_end tested only via validation logic, not an end-to-end test.
