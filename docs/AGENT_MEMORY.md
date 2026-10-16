# TransitOS agent memory

## Current dispatcher workspace (A211)

The default console uses a retro green two-panel fleet/response layout. All native fleet records are retained; evidence is inline and approvals remain visible. See docs/INTERFACE_BETA.md. The connected Databricks profile uses five local APIs and console port 8212; local rehearsal ports still use local stores. Product refresh is explicit; optional 60-second sync defaults off. No visible beta label. Frozen contracts unchanged.

## Coding-agent context

Read `AGENTS.md` at session start. This file is a compact map, not a second assumptions register:

- Current behavior: `docs/ASSUMPTIONS.md` (including A197-A206), frozen contracts, then source. Phase prompts/handoffs are historical when newer owner decisions supersede them.
- Native product evidence: `integration/product_views.py` reads existing mock APIs; `panel/console/product-views.js` renders combined/individual views. Notifications are in-app observed milestones, never invented arrival. Identity bridge reads actual current crosswalk and resolved events (A206); seeded mappings are not LLM inference. See A205 and the product-record section of `docs/DEMO.md`.
- Actual local console: `integration/console.py`, `panel/console/`, `docs/DEMO.md`. Scenario: `operator_swap_needed`; all products simulated; code checks options; humans approve plan and return; rider alert last.
- Interactive repository: `scripts/build_explorer.py`, `docs/explorer/`, generated `site/`, `docs/EXPLORER.md`. The web replay selects captured states; it does not run Python or make model calls. Source sampling is allowlisted. Do not claim hosted deployment succeeded until its URL is verified.
- Reasoning: one interface in `workflow/reasoning.py`; default stub; OpenAI `gpt-4o-mini` adapter under a persistent $0.50 app maximum; legacy Haiku optional. Keys stay in ignored local configuration, never browser code, reports or commits.
- Verify: `make verify` (offline), `make evals` (eight focused offline behavior cases), `make explorer` (evals + actual-system recording). Live OpenAI checks are opt-in and counted in the real app ledger. Old test counts are historical evidence; report only observed commands/results.
- Preserve owner files and local tooling. Do not copy external reference documents, runtime `.local/` files or journal state into public artifacts. Only journal-writer edits journal files; journal remains local.
- Databricks first product (A207-A208): owner imported 202 baseline rows into four workspace product schemas and created an empty maintenance.work_orders table. `make maintenance-databricks` starts optional mocks/databricks_maintenance.py on 8103 using browser OAuth and explicit connection environment settings; docs/DATABRICKS.md has the owner-run read/create/replay/cancel check. This service is not wired to the console; no live success may be claimed from offline tests. Owner wants to perform browser changes themselves. Keep account settings/exports in ignored .local/; frozen contracts unchanged.
- Live maintenance milestone 2026-10-15: owner authorized agent to verify the manual test. Local GET + exact POST replay (capital-D key Databricks-check:create_work_order:1, asset1106, description "synthetic connection api test", system-opened) returned same order; independent Databricks SQL confirmed exactly one simulated agency-042 work-order row, still open. Browser controls were unreliable, so HTTP/SQL completed verification. That was the first milestone; subsequent A209 acceptance verified the complete connected run, scheduling restart and cancellation. See docs/DATABRICKS.md for current evidence.

## Runtime episodic memory

`workflow/memory.py::IncidentMemory` stores summaries in `.local/agent-memory.sqlite`, scoped by agency and canonical vehicle. `MemoryReasoner` adds a bounded, whitelisted history view to the proposal context. The local console wires this wrapper into the existing reasoner; the ordinary integration default remains unchanged unless the wrapper is explicitly injected.

A memory is retained only when the actual observation contains a `DONE` run, a plan, plan and return approval scopes, a successful availability-return write, and clean invariants. A world-generation/run key prevents repeat publication from duplicating the memory. It records the trigger type, successful action names, outcome and source run; raw vendor descriptions, operator details and credentials are not retained. A new connection/reset can recall the latest three episodes for that exact agency/vehicle.

History is context, never an authority source. It does not update policy, bypass approval, decide current availability or reuse a prior assignment. The model sees only the successful outcome, trigger type and simulation marker; fresh feasible lists and the existing validator stay authoritative. Unknown data still requires acknowledgement. A broken memory store is treated as no history and cannot block/authorize execution.

Memory survives **Reset scenario** and console restart. Completed episodes from a fictional demo are not production training evidence. The static hosted explorer deliberately exports no memory episodes or database. Audit revisions and LangGraph checkpoints remain separate: memory is a small derived history view, not the audit source of truth or a new fleet system of record.

## Evaluation boundaries

The offline eval suite checks actual effects, adversarial fake model IDs, explicit acknowledgement, stale approvals, kill switch, spend refusal and memory isolation. It is not a live-model quality benchmark and does not prove production readiness. `make verify` still runs the broader 50-scenario system report, with known gaps identified. Use `make openai-smoke` only when a provider-level recheck is justified and within the unchanged app cap.

- Connected Databricks profile (A209): `make connected` / `.local/start-connected.sh <instance> stub`; five native HTTP product processes 8201–8205, console8212. Original imports are baseline; runtime api_state/live_* views are separate. OAuth cache under ignored .local is credential-bearing and owner-only. No cloud reset; instance names isolate rehearsals. See docs/DATABRICKS.md for recovery limits and measured acceptance.

- Dispatcher beta (A210): default index.html now loads workspace-model.js + workspace.js + workspace.css. Persistent queue/case, contextual drawers, fleet search, stage strip, version-bound approval and actual per-key product read-back labels. Existing console.js is legacy source; product.html still uses product-app.js/console.css. Read-only no-show/payroll previews never submit runtime commands. See docs/INTERFACE_BETA.md and docs/DEMO.md; local free beta URL is 8114 when launched. tests/panel/test_workspace_model.py runs Node presentation checks against the real offline recorder. No private client research files/assets were copied into repo.
