# Handoff: Track E (Panel and ops)

## Built
`src/transitos/panel/`: `ports.py` (Protocols + exceptions), `fakes.py` (in-memory fakes), `app.py` (FastAPI app, JSON API + server-rendered HTML, htmx via CDN as progressive enhancement; forms also work without JS). Tests: `tests/panel/test_panel.py` (8 tests).

## Run
`create_app(gateway=, crosswalk=, events=, audit=, killswitch=, unresolved=None, agencies=None) -> FastAPI` (`transitos.panel.app`). `unresolved` defaults to the crosswalk if it has `list_unresolved`. `agencies` lists agencies shown on the kill-switch page. Demo: build with the fakes and `uvicorn`.

## Public entry points
- Ports (`panel/ports.py`): `RunGateway.list_pending(agency)`, `.get_run(run_id)`, `.submit_decision(run_id, decision, plan_version, edits, user) -> RunState` (may raise `RunNotFound`, `StaleVersion`, `DecisionConflict`); `EventSink.publish(event)` (wire to producer.send(TOPIC_RAW, key, event)); `AuditReader.for_run(run_id) -> list[DecisionRecord]`, `.timeline(agency, limit) -> list[{time, run_id, kind, summary, agency}]`; `KillSwitchStore.is_on/set`; `UnresolvedReader.list_unresolved(agency) -> list[{agency, product, native_id, event_ids}]`.
- HTTP: `GET /pending?agency=`, `GET /runs/{run_id}`, `POST /runs/{run_id}/decision` (409 on stale/conflicting, 200 with `already_decided: true` on identical double submit, gateway called once), `POST /defects` (DefectReported, or VehicleFaultDetected if spn+fmi given; `data.source="human"`, deterministic ids), `GET /unresolved`, `POST /crosswalk` (CrosswalkEntry; republishes events returned by `Crosswalk.resolve`), `GET|POST /killswitch`, `GET /timeline`. HTML: `/ui/pending`, `/ui/runs/{id}`, `/ui/unresolved`, `/ui/killswitch`, `/ui/timeline`, `/ui/defect`.
- Run ids contain colons; routes use `{run_id:path}`.

## Assumptions
- A11: Contract `Crosswalk` has no listing of unresolved items, so the panel needs a read port `UnresolvedReader` (integration must implement; no contract change made).
- A12: Staleness is checked in the panel (run.plan_version vs body) and again in the gateway; idempotency is by (decision, plan_version) already recorded on the run (`RunState.decision`).
- A13: "Edit" decision passes `edits` as free-form dict; the workflow track defines its schema. The HTML form takes JSON text.
- A14: Kill switch state is stored/enforced elsewhere (workflow reads `KillSwitchStore`); the panel does not itself block decisions.
- A15: No auth; `user` is a self-reported string. Event `source` is `adapter/panel/<agency>`.

## Needs from others
Integration must supply real `RunGateway` (D), `Crosswalk`+`UnresolvedReader` (B), `EventSink` (B), `AuditReader`, `KillSwitchStore`.

## Not done
No authentication, no live polling/auto-refresh, no pagination, htmx loaded from CDN (needs internet in browser; plain form posts work without). No python-multipart dependency (form bodies parsed manually). Not tested against real tracks.

## Verification
`uv run pytest -q`: 15 passed (includes contracts tests). `ruff check src/transitos/panel tests/panel`: all checks passed.
