# Dispatcher interface: current operations workspace

The second design iteration uses the existing explorer’s retro green palette. No beta label appears in the interface. Two simultaneous panels show the full fleet/service picture and the attention queue with the selected response. Vehicle evidence is inline; approval controls stay outside the response scroll. Technical traces alone use a dialog.

The connected profile reads all 12 vehicles, 72 service blocks and 10 operators through native simulated product APIs backed by Databricks. Roster availability and recorded faults are separate facts. Native IDs, source timestamps and missing evidence remain explicit; no GPS feed is invented. Local rehearsal and recorded replay are labelled separately from Databricks.

Sync products explicitly refreshes product evidence. Optional 60-second automatic sync is off by default and runs only while the page is visible. Cached browser state polling does not query Databricks. Existing approval gates still govern workflow writes.

Validation: focused suite 32 passed; full verification 1763 passed, 181 skipped, 50/50 scenarios, data-check OK. Temporary DOM checks covered the full connected fleet, filtering, linked evidence, pinned approval, version-bound decisions and preview isolation. Native browser visual and dispatcher usability acceptance remain pending.

## Previous iteration and research rationale (historical)

# Dispatcher interface beta

## Design flow

1. Reconcile the current state/commands with the requested dispatcher tasks.
2. Inspect public reference interfaces; transfer interaction patterns, not assets or branding.
3. Compare two low-fidelity arrangements against three task families.
4. Define stable components and explicit data/command boundaries.
5. Implement the selected arrangement against the existing console snapshot.
6. Verify actual approvals, evidence, failures, preview isolation and responsive layout.
7. Owner review, then dispatcher usability sessions before calling the design validated.

## Reference observations

- Swiftly [Issues in Live Operations](https://www.goswift.ly/blog/issues-in-live-operations): inspected the published screenshot, showing selected issue beside vehicle/block/operator context. Transfer the connected entity context, not its map-first home; this POC has no GPS. No authenticated product operated.
- PagerDuty [incident navigation](https://support.pagerduty.com/main/docs/navigate-the-incidents-page): inspected published list/actions screenshot. Transfer predictable status and row selection, not bulk approval of consequential plans. No authenticated product operated.
- ServiceNow [in-product workflows](https://www.servicenow.com/community/servicenow-otto-articles/in-product-experience-for-agentic-workflows/ta-p/3507831): inspected published AI Activity screenshot (input-required and ready-for-review cards). Transfer contextual progress and input states; do not equate API acceptance with verified outcome. No authenticated product operated.
- Optibus Operations page's main “operations” image was an illustration, not interface evidence. It was excluded from visual findings.

## Task and information model

For a fault: understand the signal, affected blocks, eligible cover and checks; inspect native evidence; approve the exact version; watch effects; inspect failures/compensation; wait on repair; separately approve return. The existing graph and command API remain authoritative.

Shared entities: vehicle, block, operator, work order, alert, decision. Each keeps its native IDs, source and simulation timestamp. Missing data is unavailable, not zero. No invented riders, confidence scores, arrival or ETA measurements.

Shared shell: attention queue, case heading, current decision, impact, proposal, ordered actions, related evidence and timeline. No-show uses candidate comparison; payroll uses line-item exceptions. Those two are read-only illustrative previews, isolated from runtime commands and live counts.

## Low-fidelity alternatives

A — persistent three-pane: queue | decision + plan | all evidence. Fault review has simultaneous evidence but becomes narrow on a laptop; a 72-block table displaces the decision. Payroll needs wider tables. Persistent right evidence would dominate even when unnecessary.

B — persistent queue + wide case; related evidence opens in a contextual drawer. Decision and plan remain the anchor. Tables can use the full case width; source/entity buttons open evidence without navigation. Drawer closes to the originating button and Escape works. On narrow screens the layout stacks and the drawer becomes modal.

Selected B for beta. This is a design judgment, not a measured usability result. Maps and assistant are future working views; neither is a fabricated beta feature. No incident tabs or product-tab hopping are required.

## Component and state boundaries

- Queue groups: needs decision, in progress/waiting, completed. One actual scenario; preview items kept in a separate labelled area.
- Decision card changes by actual run status; pending, executing, repair wait, return approval, escalation and rejection are distinct.
- Stage strip shows observed graph nodes/current waits, not imaginary autonomous agents.
- Action rows match exact idempotency keys. API acceptance is labelled accepted; verified only when current native read-back supports that action's target. Later compensation is visible and supersedes forward-effect claims.
- Approval stays bound to displayed generation/plan version/scope. Unknown-check acknowledgement is drawn only from actual required rules; no arbitrary “I read it” checkboxes.
- Agency pause is labelled agency-wide. No fake case-level cancel/takeover or configurable permissions.
- Evidence drawers reuse native API snapshots. Read times are simulation times, not wall-clock freshness guarantees. Unavailable reads and identity conflicts remain visible.
- Fleet search exposes all current vehicles, blocks and operators without leaving the workspace.
- Demo controls are separate from dispatcher controls. Connected reset stays disabled. Stub/OpenAI/fallback and replay modes are stated accurately. No paid calls are needed to test the beta.

## Validation and release steps

Automated: existing full workflow/eval suites; frontend model tests against actual captured system states; exact per-block action matching, unavailable evidence, post-compensation labels, preview isolation, stale approval and code-enforced safety remain protected.

Visual/manual: ready, proposal, drawer, repair wait, return, completion and preview at desktop/laptop widths; keyboard focus, Escape, form preservation under polling and absence of horizontal page overflow.

Beta remains an internal simulated-products POC. Future scenario previews do not execute, and no external reference documents are copied into this repository. No publication is part of this change.

Owner/usability follow-up: compare evidence lookup, consequence comprehension and interruption recovery with the earlier console. Record errors and comprehension as well as clicks and time; do not invent test-participant results.

## Observed beta checks

- Focused console + connected + frontend wrapper: 30 passed in 20.76s.
- Six Node presentation checks use a fresh actual-system recording; the pytest wrapper runs them as part of make verify when Node is available.
- Temporary jsdom interaction harness: ready/proposal/repair/return/done/rejection rendering, preview isolation, input preserved through polling, drawer cancel/focus return, fleet filtering, paused approval, bound plan/scope/name, refusal message and button recovery all passed. jsdom installed only under /private/tmp; no runtime dependency added.
- Recorded connected acceptance snapshot: 12 vehicles,72 blocks,8 affected blocks; all eight native block targets and the actual workforce operator match. This is a presentation check of earlier live evidence, not another live cloud run.
- Safari visually rendered ready/proposal and the review-decision jump to the actual form. The fault was started via the beta; zero writes were shown at proposal. Native drawer AX content was observed, but browser control lost usable element frames intermittently. Complete drawer/responsive visual sign-off remains an owner review item; DOM behavior passed. No dispatcher usability study was performed.
- Static explorer built successfully into /private/tmp/transitos-beta-explorer with actual-system replay. Nothing deployed or published.

Final required verification: `UV_NO_SYNC=1 make verify` → **1761 passed,181 skipped in144.93s**, ruff clean, **50/50 scenarios**, **data-check:OK**. `UV_NO_SYNC=1 make evals` → **8/8 behavioral evals;0 paid calls**.

Live local beta acceptance used the actual workspace JavaScript event handlers in a temporary DOM harness and actual localhost HTTP requests. Explicitly labelled `Beta scripted UI acceptance` decisions passed both gates: five recovery adapter effects, repair wait, delayed rider-feed visibility, separate return decision, DONE with six writes and zero invariant violations. The working local rehearsal was then reset through its normal API and a fresh unapproved proposal requested for owner review. Evidence remains ignored at `.local/beta-ui-acceptance.json`. This is functional UI/HTTP acceptance, not a complete native-browser click-through or a live Databricks rerun.

Current review entry: http://127.0.0.1:8114 while the isolated stub server runs. Restart with `bash .local/start-beta.sh` from the project root; the short-path terminal helper also provides `bash beta.sh`. The beta uses free local simulated products; connected8212 uses the same frontend with Databricks evidence when launched.
