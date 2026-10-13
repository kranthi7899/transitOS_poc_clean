# Dispatcher demonstration

Every product and agency is simulated. The console drives one real integrated LangGraph workflow using the existing `operator_swap_needed` scenario. No real fleet system is connected.

## Launch

From the project root:

```sh
uv sync
make console
```

Open **http://127.0.0.1:8110** in Chrome. Keep the terminal running; Ctrl+C stops it. This is the free, deterministic rehearsal mode. It needs no Kafka, Postgres, frontend build or internet connection.

For OpenAI reasoning, put `OPENAI_API_KEY=...` in the git-ignored `.env.local`, then use **`make console-openai`**. The key never reaches the browser. An exported environment key takes precedence over the local file. An absent key is a configuration error. The console labels the actual reasoning mode and any code-ranked fallback.

`gpt-4o-mini` generates a strict JSON proposal through the Responses API. Code validates it and ranks feasible plans; approvals still control all writes. An API failure, refusal or invalid plan uses the validated deterministic fallback and says so. No external tools are exposed to the model. Standard prices checked on 2026-10-12: $0.15 / million input and $0.60 / million output tokens ([official model page](https://developers.openai.com/api/docs/models/gpt-4o-mini)).

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
