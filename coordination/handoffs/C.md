# Handoff — Track C (tools, adapters, registry, policy)

## Built (src/transitos/tools/)
- `read_adapters.py`: `SchedulingReader`, `MaintenanceReader`, `WorkforceReader` (workforce reads the daily CSV). Native -> canonical mapping via the Crosswalk; unmapped IDs / unknown status / bad payload / dead product -> `Result.unavailable(product, note)`. `list_ready_vehicles` is `partial` if the blocks feed is down (conflicts unchecked). `get_vehicle_history` is `partial` if maintenance is down.
- `write_adapters.py`: `MaintenanceWriter` (find-open-first, reuse human WO with `note="already_open"`, Idempotency-Key header, read-back; cancel only system-created), `SchedulingWriter`, `WorkforceWriter`, `AlertsWriter`. Every write: kill switch -> idem-store replay -> hard-limit re-check from live product state -> call -> read-back.
- `common.py`: `Http` (X-Agency header, 3s timeout, 3 retries with exponential backoff on 429/5xx/transport errors), `Deps`, `StaticCrosswalk` (in-memory Crosswalk for tests/local; Track B owns the real one).
- `idem_store.py`: `IdemStore(db_path)` SQLite; (agency, tool, key) -> prior Result. Only `ok` results are stored (a failed attempt can be retried).
- `policy.py`: `load_policy(agency, config_dir)`, `KillSwitchStore(db_path)` (`.set(agency,on)`, `.get`), `kill_switch(agency, store, policy)` (SQLite flag OR policy-file flag, read live per write), pure checks `check_min_spares`, `check_operator_hours`, `check_lift_qualification`, `check_spare_meets_needs`, `check_hard_limits(...)` (return list of violation strings, empty = ok).
- `registry.py`: `Registry(config_dir=None, clients=None, crosswalk=None, idem=None, killswitch=None, sleep=None, extra_adapters=None).toolset(agency) -> ToolSet` implementing all ReadTools + WriteTools. Config: `config/<agency>.yaml` maps tool -> adapter id (`maintenance_mock_v1`, `scheduling_mock_v1`, `workforce_mock_v1`, `alerts_mock_v1`); swapping a product = change one line / `register_adapter`. Policy: `config/policy/<agency>.yaml`. Only `agency-042` shipped (no agency-077: Track A v2 variants not available). `ToolSet` raises ValueError if called with a different agency than it was built for.
- `contract_tests.py`: `ALL_CHECKS` + `ContractCase`, reusable against any ReadTools/WriteTools implementation.

## Run
`uv run pytest -q tests/tools` ; `uv run ruff check src/transitos/tools tests/tools`

Entry points for others:
```python
reg = Registry(clients={"maintenance": c1, "scheduling": c2, "workforce": c3, "alerts": c4},  # httpx.Client / TestClient
               crosswalk=crosswalk, idem=IdemStore("data/idem.db"), killswitch=KillSwitchStore("data/ks.db"))
tools = reg.toolset("agency-042")
```
If `clients` omitted, clients are built from `base_url` in the YAML. Panel (Track E) toggles `KillSwitchStore.set(agency, True)` against the same db file (or env `TRANSITOS_KILLSWITCH_DB` for the default store).

## Behaviour Track D must know
- Blocked writes return `Result.unavailable(product="policy", note=...)`: note starts with `kill_switch` or `hard_limit: ...` (contract Result has no "rejected" status). Product outages carry the product name instead. A product 4xx rejection also comes back as `unavailable(<product>, "http 409: ...")`.
- Compensation of `reassign_block` / `assign_operator`: pass keyword `compensation=True` (ToolSet accepts it; not in the Protocol) so restoring the original assignment skips needs/min-spares/hours checks. Without it the checks run. Please add to the contract if you want it formal (request not filed; non-breaking extra kwarg).
- Write `Result.partial` with `value=None` means "write was sent but read-back failed" - treat as needing reconciliation, not retry-blind. Not cached in idem store.
- `create_work_order` returns the existing open/in_progress WO (any source) with `note="already_open"`.
- Rider alert returns `{"alert_id", "block_id"}` with `note="eventually_visible"`; no read-back (visible_after).

## New assumptions
- A11: no `GET /blocks/{id}` in MOCK_API; adapters fetch `GET /blocks` (no params) and filter by id for re-validation/read-back. Ask Track A to add `GET /blocks/{id}` or confirm params are optional.
- A12: rider alerts are served by a product key `alerts` (default same host as scheduling); alert `header_text` = first line of message truncated to 100 chars.
- A13: native vehicle status strings map {ready, available -> ready; in_service, active -> in_service; out_of_service, unavailable -> out_of_service; reserved}; WO `cancelled` maps to `closed`. Unknown -> unavailable.
- A14: crosswalk product keys used: `scheduling`, `maintenance` (asset), `telematics`, `workforce`. Block IDs and WO IDs are native pass-through (not in crosswalk).
- A15: min_spares = ready, unassigned vehicles in the block window (any needs) including the target; using one must leave >= min_spares.
- A16: operator eligibility uses hours, lift training, availability window only; `qualifications` are not matched to `needs.type` (semantics unknown).

## Not done / gaps
- Tested only against my own fakes (`tests/tools/tools_fakes.py`, written from MOCK_API.md) because Track A's mocks were not on main. Re-run `tests/tools` against the real mocks once merged; field-name drift is the likely failure.
- No agency-077 / v2 variants. Swap is proven in a test with a stub adapter.
- Writers' internal re-validation reads use the v1 readers directly, not the swapped ReadTools.
- Chaos modes (duplicate, late, timeout as actual delay) not exercised; timeouts are tested as unreachable host and 503.
- Dependencies: none added. Ruff in this environment applies a strict global ruleset; the tools dirs pass, `contracts/` itself has 4 findings (not touched).

## Test output
```
46 passed, 30 warnings in 0.47s   (uv run pytest -q; includes tests/contracts)
ruff check src/transitos/tools tests/tools: All checks passed!
```
