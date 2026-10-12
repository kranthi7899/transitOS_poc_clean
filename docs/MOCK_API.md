# Mock product HTTP API (frozen v1) — shared by Tracks A (implements), B and C (consume)

All JSON. Multi-tenant: every request carries header `X-Agency: <agency_id>`. Each mock is a FastAPI app exposing `create_app(clock, chaos, db_path) -> FastAPI`, testable with `TestClient` (no ports needed). Each mock exposes admin routes under `/sim`: `POST /sim/reset`, `POST /sim/seed` (load synthetic fleet), `POST /sim/chaos {mode, on}` (modes: timeout, http_429, duplicate, late, reject_write). Shared sim clock: `transitos.mocks.clock.SimClock` (`now()`, `advance(hours)`).
Native ID formats: scheduling `BUS-44`, telematics `T-77`, maintenance `1106` (asset), workforce `OP-310`. Canonical IDs `veh-<n>` come from the crosswalk, never from products.
Timestamps RFC3339 UTC. All list endpoints accept `updated_since` where noted.

## Maintenance (FASTER-style, polled; accepts Idempotency-Key; 429 under chaos)
- `GET /assets/{asset}` -> {asset, description, status, meter_mi}
- `GET /pm-schedules?updated_since=` -> [{asset, service_code, due_mi, meter_mi, updated_at}]
- `GET /workorders?updated_since=&asset=&status=` -> [WO]
- `GET /workorders/{id}` -> WO = {id, wo_no, asset, status(open|in_progress|closed), description, opened_by(system|human), opened_at, closed_at, updated_at}
- `POST /workorders` (header Idempotency-Key optional) {asset, description, opened_by} -> WO. Same key -> same WO, no duplicate.
- `POST /workorders/{id}/cancel` -> WO (only if opened_by=system)
- `POST /sim/workorders/{id}/close`, `POST /sim/meter {asset, meter_mi}` -> advance state (drives PMOverdue / WorkOrderClosed)

## Telematics (Samsara-style; webhook + REST)
- Outbound webhook: mock POSTs `{eventId, vehicle:{id,name}, fault:{spn,fmi,lamps:{stop,warn,protect},occurrenceCount}, time}` to the URL set via `POST /sim/webhook {url}` (signature header `X-Signature: sha256=<hmac>` with shared secret `dev-secret`, A8). Duplicates and lateness under chaos.
- `GET /faults/current?vehicle=` ; `GET /faults/feed?cursor=` -> {items:[...same shape...], next_cursor}
- `POST /dvir` {vehicle, defect_text, reported_by_role} -> {eventId}
- `POST /sim/fault {vehicle, spn, fmi, lamps}` triggers a fault.

## Scheduling (TripShot-style; NO idempotency keys; no write endpoint to cancel a block — A2)
- `GET /vehicles?status=&updated_since=` ; `GET /vehicles/{id}` -> {id, seats, wheelchair_positions, lift_ok, type, status, depot, updated_at}
- `GET /blocks?vehicle=&from=&to=` -> [{id, vehicle, operator, start, end, start_depot, needs:{seats,wheelchair_positions,lift,type}}]
- `GET /status-changes?since=` -> [{vehicle, status, changed_at, changed_by}]
- `POST /vehicles/{id}/unavailable` {start, end} ; `POST /vehicles/{id}/available`
- `POST /blocks/{id}/reassign` {vehicle} (rejects if spare lacks needs or is taken; chaos `reject_write`)

## Workforce (daily CSV export for reads; flaky write)
- `GET /export/operators.csv` columns: operator,qualifications(;-sep),lift_trained,hours_today,hours_week,available_from,available_to,on_extra_board
- `GET /operators/{id}` ; `POST /blocks/{id}/operator` {operator} (may reject under chaos) ; `GET /availability-changes?since=`

## Rider alerts (GTFS-RT service-alert style; eventually visible)
- `POST /alerts` {block, header_text, description_text} -> {id}; `GET /alerts` shows alerts only after `visible_after` (sim clock). `POST /alerts/{id}/correction`.

## Event ownership (which mock source produces which event) is in `transitos.contracts.events.PUBLISHER`.

## v1.1 clarifications (integration; verified by tests/integration against the real mocks)
- `updated_since` / `since` filters are inclusive (`>=`): consumers dedupe by deterministic event id (outbox UNIQUE).
- Maintenance: cancelling a system WO sets `status=closed` plus `cancelled: true`; a cancelled WO is not a repair (no `WorkOrderClosed`).
- Telematics DVIR on the webhook/feed: `{eventId, vehicle:{id,name}, dvir:{defect_text, reported_by_role}, time}` (no `fault` key).
- Scheduling `POST /vehicles/{id}/unavailable|available` accept optional body `changed_by` (default `api`), echoed in `/status-changes`. TransitOS sends `transitos:<idempotency key>`.
- Workforce `GET /availability-changes?since=` items: `{operator, status, changed_at}`. `GET /assignments/{block}` -> `{block, operator}` is the system of record for who drives a block (scheduling's `operator` is not updated by `POST /blocks/{id}/operator`).
- Workforce CSV / `GET /operators/{id}`: `available_from` / `available_to` are RFC3339 UTC on the current sim day.

## Phase 10 additions (all SIMULATED; additive; defaults unchanged)
- **Identity evidence:** scheduling vehicles, telematics events and maintenance assets carry optional `vin`, `plate`, `name` (messy on the generated agency `agency-088`; absent or exact on the older agencies). Maintenance assets can be `status: retired` (a new work order answers 409).
- **Maintenance Pending Repair:** `POST /pending-repairs` (Idempotency-Key honoured), `GET /pending-repairs[?asset=&status=&idempotency_key=]`, `GET /pending-repairs/{id}`, `POST /pending-repairs/{id}/cancel`; foreman sim routes `POST /sim/pending-repairs/{id}/convert` and `POST /sim/workorders/{id}/status`. Repair statuses `OPEN`, `IN_PROGRESS`, `WAITING_PARTS`, `PENDING` (deferred), `COMPLETE`; work order `CLOSED`. Every pending-repair response carries `"simulated": true`.
- **Wire-format knob (default OFF):** maintenance renders zone-less local time (optionally with a per-field `utc_offset`), the workforce CSV export renders local time, telematics sends epoch milliseconds. Adapters normalise to UTC; an unknown agency timezone is never guessed (reads answer unavailable, note `timezone_unknown`).
- **Workforce:** the export gains licence class, endorsements, trained vehicle types, training and medical validity dates, last duty end, duty start; the token `none` marks a list the product knows is empty (a blank cell means "not recorded"). A switch can blank cells to test missing data.
- **Scheduling:** blocks declare `max_vehicle_length_ft` (null = none). The product stays permissive about vehicle class on purpose (policy decides in `clearance`).
