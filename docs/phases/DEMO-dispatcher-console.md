# Dispatcher console: one-scenario demonstration

Owner scope: a working browser demonstration of the orchestration vision, ready for rehearsal. Dates in the external references are context, not requirements. Every product is simulated. Contracts remain frozen.

## Story and acceptance

Use the existing `operator_swap_needed` fleet and scenario. A signed telematics fault for BUS-44 resolves to the same vehicle in maintenance and scheduling. No product writes occur until a dispatcher approves a concrete plan. The approved workflow opens a work order, holds the bus, reassigns the block and operator, then alerts riders. A simulated shop close produces the real maintenance event; a second approval returns the bus to service. The final audit and invariant results are visible.

## Interface plan

1. **Operations shell.** Navy navigation, light working area, teal orchestration accent. Header shows fictional agency, simulated clock, simulation badge, reasoner mode and agent pause state. A compact Needs Attention card shows the incident, impact, approval state and next action. Counts come from the demo state, never invented fleet-wide metrics.
2. **Incident workspace.** BUS-44, fault evidence and cross-product IDs; affected block, current assignment, proposed spare and operator; explanation, ranking and alternate plans; understandable safety checks and explicit acknowledgements. Approve/reject and dispatcher identity are next to the proposal. Show zero writes before approval. Approved decisions are version-bound; stale decisions fail rather than apply to a reset run.
3. **Execution.** Five product lanes show queued/completed/failed actions and their actual results. The rider alert is last. Show work-order number, held vehicle, changed vehicle/operator, and actual alert visibility. Preserve completed state after it leaves the approval queue.
4. **Repair and return.** A separately labelled Demo controls area triggers a fault, completes repair with simulated time advancement, advances alert visibility and resets the isolated world. Return requires a second approval. Reset confirms intent and does not reset accumulated model spend.
5. **Orchestration and audit.** Show actual LangGraph node updates and checkpoint wait states, not invented agent conversations. Separate model proposal, code checks, human approval and adapter actions. Audit shows human decisions, writes, compensation flags and idempotency details. A Vision view explains follow-on workflows, clearly labelled future possibilities.
6. **Recording usability.** Responsive layout, keyboard controls, legible contrast, no external fonts/CDN dependency, visible errors, busy-state protection, automatic state refresh and a guided recording checklist. The existing panel remains available for advanced identity/fault controls.

## Implementation

- Add a local-only integrated console launcher in `integration/console.py`, with a single serialized worker operating the existing System. Cache snapshots for read-only browser polling. Stream real graph updates through a transparent wrapper so node activity can be observed without changing workflow rules or interrupt/write boundaries.
- Add buildless static assets under `panel/console/`; no frontend framework or cloud hosting is needed. Use JSON endpoints with same-origin mutation protection; bind localhost. This remains a POC, not an authenticated production console.
- Add `workflow/reasoning_openai.py` behind the existing reasoner factory. Use `gpt-4o-mini`, strict JSON Schema output through Responses, validated by existing clearance/plan logic. No tools are exposed to the model.
- Persist OpenAI spend separately from demo state. Default and hard maximum for this demo: $0.50. Reserve a conservative maximum before every request, disable hidden retries and retain reservations on uncertain failures. A $120 organisation ceiling never widens this app budget. Default verification remains offline and free.
- Preserve Anthropic compatibility, add OpenAI-specific commands, and update documentation/assumptions to supersede the old Haiku-only restriction at the owner's direction.

## Verification and delivery

Test the real console path: trigger -> approval -> five effects -> repair close -> return approval -> DONE. Test reject, duplicate/stale decisions, missing acknowledgement, reset isolation, mutation origin protection and budget refusal/error accounting. Review implementation for secret exposure, invented status, concurrency and compensation regressions. Run `make verify`, then browser-check the real console. Perform a small live OpenAI rehearsal only within the cumulative cap. Commit only intended source/docs/tests (no keys, reference documents, local tooling or journal files), then push to the existing GitHub remote. Provide launch commands and a recording runbook.
