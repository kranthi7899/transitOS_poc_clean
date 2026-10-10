# Handoff: Track B (Event plumbing)

## Built (src/transitos/plumbing/, tests/plumbing/)
- `bus_sqlite.py`: `SqliteBus(db_or_path)` implements Producer+Consumer. Global autoincrement offset (total order, so ordered per key), per-group committed offsets (commit is monotonic, poll never advances), replay via `reset_group(topic, group, 0)`, `messages(topic)`, `find(topic, event_id)`. `KafkaBus` stub raises NotImplementedError.
- `outbox.py` (table, `add(conn, event, topic, key, dedupe_id=None)` inside caller tx, UNIQUE(event_id)); `relay.py` `Relay(db, producer).run_once()/drain()` (send -> flush -> mark sent; `after_flush_hook` test seam).
- `watch/`: `MaintenancePoller`, `SchedulingPoller`, `WorkforcePoller`, `TelematicsBackfillPoller` (all `Poller(db, httpx_client, agency).poll() -> int new outbox rows`), `WebhookReceiver(db, agency, secret="dev-secret")` with `.handle(body, signature)`, `.router()`, `.app()` (POST /webhook), `sign(secret, body)`. Cursor + last-seen + outbox rows commit in one tx; upstream HTTP errors leave state untouched (`last_error`).
- `crosswalk.py`: `SqliteCrosswalk(db, bus=None)` implements the contract; extras: `unresolved()`, `resolve_in(conn, entry)`. `add_unresolved(..., event=None)` takes an optional event body; without it resolve() recovers the body from events.raw if `bus` was given.
- `crosswalk_service.py`: `CrosswalkService(db, bus, crosswalk).run_once()/drain()`; `apply_mapping(entry)` = add mapping + republish parked events to events.raw (atomic, via outbox). This is the call the panel's "fix mapping" action should use (not bare `resolve()`, which only returns the events).
- `audit.py`: `AuditConsumer(db, bus, "raw"|"resolved")` (groups `audit-raw`/`audit-resolved`), `DecisionStore(db_or_path)` with `record_decision(DecisionRecord)` and `list_for_run(run_id)` (append-only, triggers block UPDATE/DELETE; revisions are new rows, latest last). Module-level `record_decision(db_path, rec)` / `list_for_run(db_path, run_id)` helpers also exist.

## Run
`uv run pytest -q tests/plumbing`. Typical wiring: one sqlite path; `Db(path)`, `SqliteBus(db)`, `Relay(db, bus)`, pollers, `CrosswalkService`, audit consumers; each tick: pollers `.poll()`, `relay.drain()`, `svc.drain()`, audit `.run_once()`.

## Output conventions other tracks rely on
- events.raw key = `<product>:<native_id>`. events.resolved key = canonical vehicle id (`veh-n`) or canonical operator id; event `id` is unchanged from raw.
- Resolved vehicle events: `data["refs"]` = `VehicleRefs` dump; CloudEvent extension attribute `canonicalid`. Operator events: `data["operator_canonical"]`.
- events.failed: original event + extension `failurereason`; key `<product>:<native_id>`.
- Outbox dedupe ids: `resolved:<id>`, `failed:<id>`, `republish:<id>:<rand>`.

## Test result
```
$ uv run pytest -q
........................                                                 [100%]
24 passed in 0.21s
$ uv run ruff check src/transitos/plumbing tests/plumbing
All checks passed!
```
All required tests exist in tests/plumbing/test_plumbing.py (crash mid-poll, relay crash after send + consumer dedupe, raw replay, per-vehicle ordering over two products, unknown vehicle parked then fixed, HMAC reject, duplicate webhook). Mocks used: local fakes in tests/plumbing/fakes.py (Track A not on main at the time).

## Assumptions (new)
- A11: PMOverdue fires on transition to `meter_mi > due_mi` (strictly) per (asset, service_code, due_mi). WorkOrderOpened fires on first sight of a WO (also if first seen already closed, plus Closed).
- A12: `/availability-changes` item shape is `{operator, status, changed_at}` (not specified in MOCK_API.md).
- A13: DVIRs reach us on the webhook/feed as `{eventId, vehicle:{id}, defect:{text, reportedByRole}, time}`; MOCK_API.md only specifies `POST /dvir -> {eventId}`. Track A must emit something compatible or tell me the shape.
- A14: Vendor `eventId` is stable across webhook and feed for the same fact (reconciliation key). Fault event id = `fault_id(agency, vehicle, spn, fmi, time)`.
- A15: PM/WO `updated_since` and `since` are treated as inclusive-safe: re-reads are harmless because of outbox UNIQUE.
- A16: PMOverdue/WorkOrder* native asset ids (maintenance product) map to canonical vehicles through the same crosswalk table (entity vehicle).

## Needs from others
- Track A: confirm A12/A13 shapes; seed crosswalk entries via `SqliteCrosswalk.resolve(CrosswalkEntry(...))` or `CrosswalkService.apply_mapping`.
- Program lead: decide whether `refs`/`canonicalid` should be formalised in contracts (request file not written; works as extra fields).

## Not done
- No integration run against Track A's real mocks; no scheduler/loop runner (callers drive ticks); no Kafka; no webhook agency routing beyond one receiver per agency; no retry/backoff on upstream errors beyond "try next tick"; no pruning of bus/outbox tables.
