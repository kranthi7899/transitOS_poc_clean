# Dispatcher demonstration

Every product and agency is simulated. The console drives one real integrated LangGraph workflow using the existing `operator_swap_needed` scenario. No real fleet system is connected.

## Current operations interface

The default interface shows fleet monitoring and incident response side by side. Select a vehicle to inspect its native evidence without leaving the decision. Sync products refreshes evidence; automatic sync is optional and off initially. Existing approvals, source labels and simulation markers remain visible.

For the full imported synthetic fleet, use the opt-in connected profile on **http://127.0.0.1:8212** after configuring the environment described in `docs/DATABRICKS.md`. Local rehearsal below uses a smaller local fixture, not the imported Databricks dataset. The hosted explorer is a recorded replay.

## Launch

From the project root:

```sh
uv sync
make console
```

Open **http://127.0.0.1:8110** in Chrome. Keep the terminal running; Ctrl+C stops it. This is the free, deterministic rehearsal mode. It needs no Kafka, Postgres, frontend build or internet connection.

For OpenAI reasoning, put `OPENAI_API_KEY=...` in the git-ignored `.env.local`, then use **`make console-openai`**. The key never reaches the browser. An exported environment key takes precedence over the local file. An absent key is a configuration error. The console labels the actual reasoning mode and any code-ranked fallback.

`gpt-4o-mini` generates a strict JSON proposal through the Responses API. Code validates it and ranks feasible plans; approvals still control all writes. An API failure, refusal or invalid plan uses the validated deterministic fallback and says so. No external tools are exposed to the model. Standard prices checked on 2026-10-12: $0.15 / million input and $0.60 / million output tokens ([official model page](https://developers.openai.com/api/docs/models/gpt-4o-mini)).

## Live presentation launch

From the project folder, run **`make present`** and open **http://127.0.0.1:8112**. Keep the terminal running. This uses the configured OpenAI key and the existing $0.50 app cap; startup itself makes no paid request. Stop an existing server with Ctrl+C before starting another on this port. `make present-free` uses the same URL with the deterministic stub for a free/offline rehearsal; describe the mode shown on screen accurately.

For another take, use **Operations → Reset scenario**. Reset clears this isolated product world, incident and dispatcher updates; it preserves spend and successful incident memory. Start with zero product writes and an enabled **Inject vehicle fault** button.

Presentation order: **Product workspaces / Identity bridge → Operations / fault and proposal → Orchestration / approval wait → recovery approval → product changes and Dispatcher updates → completed repair and separate return approval → Decision audit / DONE**. Keep Scheduling and Maintenance open in their own tabs to show actual native records changing. The hosted Pages site remains a recorded replay.

## Budget

The OpenAI demo has a **$0.50 cumulative application ceiling**, even if `TRANSITOS_LLM_CAP_USD` is larger. Set that variable lower to tighten it. This protects the owner's stated $4.99 available credits; the organisation's $120 ceiling is not the demo budget. The app cannot inspect account credit balance or spend by other applications.

Each request atomically reserves a conservative input/output maximum in `.local/openai-spend.sqlite` before sending. Usage settles the amount afterward; uncertain failures keep the reservation. No hidden HTTP retries. Workflow retries reserve independently. Resetting/restarting the console preserves this ledger. Do not delete it between rehearsals. `make openai-spend` shows its status. The figure is an app estimate including conservative reservations, not an account billing statement.

`make openai-smoke` performs one paid scenario against the actual provider and requires a validated model answer (fallback is a test failure). `make verify` remains offline and makes no paid calls. No need to run the entire 50-scenario suite against a paid model for this demonstration.

## Recording sequence

1. **Operations:** explain Needs Attention, agency reserve, actual product state and Level 2 approval. Click **Inject vehicle fault**. This sends a signed webhook from simulated telematics, not a fabricated dashboard incident.
2. **Proposal:** show BUS-44 / T-77 / maintenance asset 1106 as one vehicle. Review the affected block, suggested spare/operator, explanation and safety checks. Native passes come from the same pure clearance module and the workflow's captured facts. Unknowns remain explicit. Expand evidence to inspect optional rules not evaluated, ranking and alternatives.
3. Enter the dispatcher name and click **Approve recovery plan**. Before approval, the product-write count is zero. Afterward show five recorded effects: work order, vehicle hold, block reassignment, operator assignment, rider alert last.
4. Click **Advance clock 5 min** to show the alert in the simulated rider feed. Publishing and eventual visibility are distinguished.
5. **Orchestration:** show actual LangGraph node updates, code checks, approval interrupt and repair interrupt. These are graph nodes, not claims of separately deployed domain agents. The workflow is waiting for a real mock-product event.
6. **Operations:** click **Simulate completed repair**. This advances simulated time 48 hours and closes the maintenance work order. The maintenance poller emits the close event. Click **Approve return to service**; repair completion alone never returns the vehicle.
7. **Decision audit:** show two human approval scopes, adapter results, idempotency keys and stored decision revisions. Confirm zero invariant violations and final `DONE` state. Recovery doesn't move the replacement block back to BUS-44; it restores the repaired bus's availability.
8. **Platform vision:** explain the reusable identity, adapters, policy and durable workflow foundation. Follow-on ideas are clearly labelled future possibilities, not features demonstrated today.

Use **Reset scenario** for another take; the dialog confirms clearing this isolated simulated world. Pause/resume is an agency kill-switch control between demo commands. The worker serializes actions; a busy action must finish before another command is accepted. Full production authentication, concurrent operator access and real vendor integration remain future work.

## Validation

Console integration tests exercise actual System writes, rejection, stale/double approval, missing acknowledgement, kill switch and reset isolation. OpenAI tests use an HTTP fake to verify strict schema, spend accounting and failures without cost. Full verification results and the live rehearsal result are reported at delivery, not inferred from prior runs.

## Delivery checks (2026-10-13)

Local `TRANSITOS_REASONER=stub make verify` output observed during this build:

```text
All checks passed!
1699 passed, 181 skipped
50/50 scenarios passed
data-check: OK
```

One live OpenAI rehearsal: `TRANSITOS_LIVE_OPENAI=1 ... pytest -q tests/llm/test_live_openai.py` -> `1 passed`; validated model answer, no fallback, full operator-swap scenario finished DONE. The app ledger afterward was $0.0034653 including retained reservations for the sandbox-blocked attempt. These are application estimates, not account billing totals. Kafka/Postgres were not rerun for this local console change.

Code review fixes: stale return approval now refuses before resuming; native clearance passes are shown from captured workflow facts; uncertain request costs survive failures; corrupt-ledger connections close cleanly; OpenAI keys are included in the tracked-file secret scan; stale browser commands after reset cannot affect a new incident.

## Explore and retain context

`make explorer-serve` opens a zero-cost static repository explorer and recorded dispatcher replay at http://127.0.0.1:8111. Use `make console` for actual local execution. The console’s Incident memory view shows successful, separately approved incidents retained across resets; every new incident still needs fresh checks and new approval. Runtime memory is local and excluded from the explorer. See [EXPLORER.md](EXPLORER.md) and [AGENT_MEMORY.md](AGENT_MEMORY.md).

## Show the product records changing

Open **Product workspaces → All products** for the dispatcher estate board. Select an individual product, then **Open in its own tab** to keep Scheduling or Maintenance beside the dispatcher. Each original workspace reads an existing independent FastAPI mock application's native API and store. All are hosted together for the local demo; these are not real vendor frontends or separately deployed production platforms. Expand **Inspect native records & format** to show JSON field names and IDs versus the workforce CSV export.

Before the fault, Scheduling shows BUS-44 on blk-1. After recovery approval, the block shows BUS-45 and Workforce shows OP-2; Maintenance has the open work order for asset 1106. The workflow waits for repair while that replacement assignment stands. Physical arrival/GPS is not tracked, so the update says assignment confirmed. After **Simulate completed repair**, Maintenance shows the closed work order while Scheduling still shows BUS-44 out of service. Only **Approve return to service** changes BUS-44 to ready in the roster; blk-1 remains on BUS-45.

**Dispatcher updates** and the latest-update strip report evidence-backed milestones without external messages. Updates accumulate in this demo session, deduplicate, and reset with the world. Product tabs poll the cached system snapshot; viewing a product cannot authorize actions. The hosted product pages follow recorded dispatcher replay states in the same browser and are labelled accordingly.

Product-workspace delivery verification (2026-10-13): `TRANSITOS_REASONER=stub make verify` → **1715 passed, 181 skipped in 94.34s; 50/50 scenarios; lint clean; data-check OK**. `make explorer` → **8/8 behavioral evals, zero paid calls**. Earlier console results above are historical.

## Explain identity before the fault (A206)

Start in **Product workspaces → All products → Identity resolution**. The Identity bridge reads the actual crosswalk: T-77 in telematics, asset 1106 in maintenance and BUS-44 in scheduling map to veh-1. Inspect each product's local identity card and the native asset register. OP-2 is a workforce-native operator ID mapped to op-2; this scenario does not give scheduling or alerts their own operator IDs. Those products join through the service block.

Inject the fault, then return to the Identity bridge to see the actual resolved fault event, canonical key and destination references. Explain the two directions: native observation → adapter → event → identity resolution → workflow; approved response → controlled tool → native product API → read-back. The crosswalk is an identity map, not the communication transport. These demo mappings are seeded and checked for agency/time validity; a conflict needs human review, never a model guess. Separate Git branches are development organization, not proof of separate deployed platforms.

Identity-view verification (2026-10-14): `TRANSITOS_REASONER=stub make verify` → **1717 passed, 181 skipped in 94.59s; 50/50 scenarios; lint clean; data-check OK**. Targeted console tests: **10 passed**. Offline evals: **8/8 passed, zero paid calls**. Chrome verified native mappings and the actual fault-event destination references before approval.

## Dispatcher beta workspace

The default console now uses a persistent attention queue and a case workspace.
Product records, identity mapping, rule checks, alternatives, graph activity and
history open in an evidence drawer rather than requiring product tabs. Search the
fleet from the header or press `/`; Escape closes evidence. A “Review decision”
button jumps to the version-bound approval form in the same case.

Use `TRANSITOS_REASONER=stub .venv/bin/python -m uvicorn transitos.integration.console:create_app --factory --host 127.0.0.1 --port 8114 --log-level warning`
for an isolated, free beta rehearsal, then open http://127.0.0.1:8114.
The same frontend is served by the connected Databricks launcher at 8212; cloud
requests consume trial compute. The 8114 rehearsal uses local simulated products.

Start the vehicle fault, inspect the plan, approve recovery, advance the clock for
rider-feed visibility, simulate repair closure, then separately approve return.
Rehearsal controls are expandable in the queue. Reset is unavailable on Databricks
instances and never clears model spending or retained incident memory.

Operator no-show and payroll items are read-only illustrative design previews,
not executable scenarios. They do not change real queue counts or submit decisions.
Product acceptance is distinct from current native verification; earlier withdrawal
is labelled superseded after return. No arrival, GPS, delivery receipt or unprovided
passenger count is invented. See docs/INTERFACE_BETA.md for rationale and boundaries.
