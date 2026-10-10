# Handoff — Track A (Mock world)

## Built (src/transitos/mocks/, tests/mocks/)
All five SIMULATED products per docs/MOCK_API.md, plus clock, chaos, seed, crosswalk seed, runner.
`uv run pytest -q` -> `54 passed in 0.56s` (contracts 8 + mocks 46); `ruff check src/transitos/mocks tests/mocks` clean.

## Run
- Tests: `uv run pytest -q`. Servers: `uv run python -m transitos.mocks.run` (8101 scheduling, 8102 telematics, 8103 maintenance, 8104 workforce, 8105 alerts; one process, shared SimClock, both agencies seeded).
- In-process: `from transitos.mocks.run import build_all; apps = build_all(clock)` -> dict name->FastAPI app.

## Public entry points
- `mocks/{scheduling,maintenance,workforce,alerts}.create_app(clock: SimClock, chaos: Chaos, db_path=None) -> FastAPI`; `telematics.create_app(clock, chaos, db_path=None, sender=None)`. `db_path=None` = in-memory SQLite.
- `mocks/clock.py`: `SimClock(start=None)` `.now()`, `.advance(hours=0, minutes=0)`, `.iso()`; helpers `iso(dt)`, `parse_ts(s)`. Default start 2026-01-05T06:00Z.
- `mocks/chaos.py`: `Chaos().set(mode, on)`, `.on(mode)`, `.clear()`; `MODES`.
- `mocks/seed.py`: `AGENCIES`, `build_fleet(agency, now) -> Fleet`.
- `mocks/crosswalk_seed.py`: `crosswalk_entries(agency) -> list[CrosswalkEntry]`, `all_crosswalk_entries()`. Products: scheduling/telematics (entity vehicle), maintenance (entity asset), workforce (entity operator); canonical `veh-<n>` / `op-<n>`.
- `mocks/telematics.py`: `sign(body: bytes) -> "sha256=<hex>"`, `verify(body, signature)`, secret `dev-secret`; HMAC is over the exact raw request body bytes (compact sorted-key JSON).
- Every mock: `X-Agency` header required (400 otherwise). Admin: `POST /sim/reset` (agency header = that tenant only; no header = all + chaos cleared), `/sim/seed` (agency header), `/sim/chaos {mode,on}`, `GET /sim/clock`, `POST /sim/advance {hours|minutes}`.
- Extra sim routes: maintenance `POST /sim/workorders/{id}/progress`, `POST /sim/config {auto_close_hours, miles_per_hour}`; telematics `POST /sim/flush`, `POST /sim/fault/clear`; scheduling `POST /sim/vehicle-status {vehicle,status,changed_by}` (a human dispatcher change); workforce `POST /sim/operator {operator, status?, <csv fields>}`, `GET /assignments/{block}`; alerts `POST /sim/config {visibility_delay_min}`.

## Seed (agency-042)
12 vehicles (veh-1..12; veh-10/11/12 spares), 72 blocks (9 line vehicles x 8, 2h each from 05:00 UTC of the sim day), 10 operators. veh-1 = BUS-44 / T-77 / asset 1106; operators OP-310..OP-319 (OP-312 has 9.5h today; OP-319 is extra board; two lack lift training). Spares: veh-10 (BUS-53) lift/2 wheelchair — the only suitable lift spare; veh-11 (BUS-54) small cutaway no lift; veh-12 (BUS-55) standard no lift. veh-3 (asset 1108, BUS-46) PM-A at 24900/25000 mi (near overdue). agency-077 = same shape, different ID formats (V101, SM-5001, A2001, E701) — formats only, no field renames.

## Quirks as implemented
- Mismatched IDs: scheduling `BUS-44`, telematics `T-77` (name "Bus 44"), maintenance `1106`, workforce `OP-310`; scheduling blocks carry the workforce-native operator id.
- Maintenance: Idempotency-Key per agency -> same WO returned; no key -> no dedupe; `http_429` returns 429 + Retry-After on all non-sim routes and creates nothing. Sim-clock drive: `/sim/config miles_per_hour` accrues meter as the clock advances (-> pm-schedules overdue); `auto_close_hours` closes open WOs after sim hours (-> status closed, updated_at moves). State refreshes lazily on every request. `/sim/meter`, `/sim/workorders/{id}/close` explicit.
- Scheduling: no idempotency (repeat `unavailable` appends duplicate status-changes); no block-cancel route; reassign 409 with reason for lacking seats/wheelchair/lift/type, target out_of_service, same vehicle, or overlapping block; `reject_write` -> 503 on reassign/unavailable/available.
- Telematics: webhook push (sender is httpx.post by default, injectable via `create_app(sender=...)` or `app.state.sender` for tests), `duplicate` = delivered twice with same eventId (also twice in feed), `late` = visible 30 sim-min later (out of order); late events are pushed on the next request or `POST /sim/flush` after advancing the clock; failed sends are retried later.
- Workforce: CSV reads; `reject_write` -> 503 on assignment; the mock does NOT enforce hours/qualification (tools code must).
- Alerts: visible only after `visible_after` (default +5 sim-min; `late` x6); no idempotency; correction creates a new alert (`corrects`=orig id) that supersedes the original once visible.
- `updated_since` / `since` filters are inclusive (>=): consumers must dedupe via deterministic event IDs.

## New assumptions
- A11: DVIR is also pushed via the webhook as `{eventId, vehicle:{id,name}, dvir:{defect_text,reported_by_role}, time}` (no `fault` key); consumers map presence of `dvir` to DefectReported.
- A12: Cancelling a system WO sets status=closed plus extra field `cancelled: true` (status enum has no cancelled); a poller will see it as closed — Track B/C must check `cancelled` before emitting WorkOrderClosed.
- A13: Telematics fault `eventId` is per occurrence (`evt-<n>`); occurrenceCount increments for the same vehicle+spn+fmi until cleared. Deterministic event IDs (contracts/ids.py) are the plumbing's job.
- A14: Scheduling block `type` need is a minimum class (cutaway < standard < articulated).
- A15: Telematics `late` delivery needs a clock advance plus any request/flush to trigger (mocks do not watch the clock themselves).
- A16: Chaos is per Chaos object (global across tenants within a mock); `http_429` affects maintenance only.

## Not done / gaps
- agency-077 has no field-name variation. No auth on products other than the telematics HMAC. Timeout chaos returns 504 immediately (no real hang). `run.py` ports are fixed; shared-process only (chaos set on one port affects only that product). Timestamps have 1-second resolution.
- Needed from others: nothing; no contract change requests.
