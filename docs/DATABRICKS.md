# Databricks synthetic product connections

All products and records are SIMULATED. Two opt-in profiles are available: standalone maintenance on port 8103, and five product APIs on ports 8201–8205 with the connected operations console on 8212. The latter displays the full imported synthetic fleet through native product APIs. Ordinary local presentation commands continue to use local stores. See the connected-profile section below for setup and boundaries.

## Imported baseline

`scripts/export_databricks_seed.py` exports fresh agency-042 mock API snapshots to
ignored `.local/databricks-seed/`. Four owner-run SQL files load seven Delta tables:
12 telematics vehicles; 12 maintenance assets and 12 PM schedules; 12 scheduling
vehicles and 72 blocks; 10 workforce operators and 72 assignments. The simulation
day is **2026-01-05 UTC**, not the day of import. SQL inserts missing agency/native
keys only and never replaces matching records. No active faults/work orders or
identity guesses are generated. The owner verified all 202 rows after importing.

The first maintenance destination additionally requires this empty Delta table:

```sql
CREATE TABLE IF NOT EXISTS workspace.maintenance.work_orders (
  agency STRING,
  is_simulated BOOLEAN,
  id STRING,
  wo_no STRING,
  asset STRING,
  status STRING,
  description STRING,
  opened_by STRING,
  opened_at STRING,
  closed_at STRING,
  updated_at STRING,
  cancelled BOOLEAN
) USING DELTA
COMMENT 'SIMULATED TransitOS work orders';
```

The owner created it and observed a zero count. This service uses the same table;
there are no new token, counter or idempotency tables to create.

## Start the local maintenance API

From the repository root, set the non-secret connection details from
**SQL Warehouses → your warehouse → Connection details**:

```sh
export DATABRICKS_SERVER_HOSTNAME='your-workspace.cloud.databricks.com'
export DATABRICKS_HTTP_PATH='/sql/1.0/warehouses/your-warehouse-id'
make maintenance-databricks
```

The command installs the optional connector using the locked dependencies, opens
Databricks browser OAuth sign-in when required, and checks the existing table
columns with read-only queries. It may start the warehouse and consume trial
credits. It never seeds, resets or creates work orders at startup. No OpenAI calls.
Do not put tokens in the repository or paste them into chat.

Open **http://127.0.0.1:8103/docs** when the terminal prints `Ready`.
Keep that terminal running. Only one local service may use port 8103; stop the old
maintenance mock first if it is occupying that port. The service is bound to
127.0.0.1, with one process and one worker. This is a trusted local demonstration,
not an authenticated/public production API.

## Owner-run proof of writing through the API

1. In the API docs, expand **GET /assets**, choose **Try it out**, set `x-agency`
   to `agency-042`, and execute. Expect 12 assets, including maintenance asset `1106`.
2. Expand **POST /workorders**, choose **Try it out**, and set:
   - `x-agency`: `agency-042`
   - `idempotency-key`: `databricks-check:create_work_order:1`
   - Request body:

   ```json
   {
     "asset": "1106",
     "description": "Synthetic maintenance API connection test",
     "opened_by": "system"
   }
   ```

3. Execute once. Expect an open work order with a stable `wo-...` ID. Execute again
   with the **same key and body**: expect that same order, not a second one.
4. In Databricks SQL Editor, run:

   ```sql
   SELECT id, wo_no, asset, status, description, opened_by, opened_at
   FROM workspace.maintenance.work_orders
   WHERE agency = 'agency-042' AND is_simulated = TRUE
     AND description = 'Synthetic maintenance API connection test';
   ```

   Expect one row. This proves a manual API write, not a model decision or approval.
5. To compensate the test, use **POST /workorders/{wo_id}/cancel** with the returned
   ID and `x-agency=agency-042`. The row remains visible with status `closed` and
   `cancelled=true`. Repeating cancellation leaves it closed. A human-opened order
   cannot be cancelled by this route. **POST /sim/workorders/{wo_id}/close** instead
   represents simulated repair completion and can close human-opened orders.
6. Stop the service with Ctrl+C and stop the SQL warehouse in Databricks. Repeating
   the test key later returns the same (now closed) order. Choose a new test key
   deliberately if another independent demonstration is needed.

## Behavior and boundaries

- API paths/JSON retain the maintenance reader/writer shape used by the existing
  tool adapters. The connected service requires `Idempotency-Key` on create.
- The agency and full key determine a SHA-256 work-order ID. Insert-only MERGE
  persists that identity with the work order itself, avoiding a separate marker
  that could be lost after commit. Reusing a key with different content is 409.
- Mutations are serialized within this service. Run one instance only; Delta
  uniqueness/multi-writer coordination across other services or manual SQL writers
  is **not** established by this POC.
- Reads and mutations filter by agency and `is_simulated=true`. Unknown/duplicate
  IDs refuse; retired assets cannot receive new orders. SQL values are parameters;
  catalog/schema identifiers are validated. No local data fallback hides outages.
- A cloud failure returns 503 and may follow a committed write. Retry the exact
  same create key/body; read-back is authoritative. No hidden write retry occurs
  inside this service. The existing tool HTTP retry behavior remains unchanged.
- The clock stays at the existing fixed simulation time; it does not claim real
  elapsed time. There is no automatic mileage accrual, shop-status progression,
  pending-repair workflow, seeding/reset or simulator clock advancement here yet.
- Hard limits, kill switch, orchestration and human approval stay upstream in their
  existing layers. Calling a product API manually is not approval-gated execution.
  The new service neither calls OpenAI nor changes workflow contracts.
- `/health` is process/configuration status only and explicitly says that it did
  not query the database. Startup preflight and successful native reads establish
  database connectivity at the time they run.

## Verification

Live connection check observed on 2026-10-15 (owner's local date): the owner created
one system-opened synthetic order for asset `1106`, description
`synthetic connection api test`, key `Databricks-check:create_work_order:1` (capital
`D`, significant). At the owner's explicit request, the agent read the local API
and replayed that exact key/body. Both returned
`wo-42457dc35921d9ca5d7af59f58cef3b0478a5749a3320457e69850c2c47568ad`,
status `open`. An independent read-only Databricks SQL connector query then found
exactly one simulated agency-042 work-order row with the same ID/asset/status.
Chrome UI control was unreliable, so the completed check used the native local
HTTP API and direct SQL rather than claiming a browser query succeeded. This
verifies live persistence and replay deduplication only. No cancellation, restart,
OpenAI invocation or approval-gated orchestrator run was performed in this check.
The original check left the order open; the subsequent delegated integration cancelled it (see below). Verification script/output stay under ignored `.local/`.

`tests/mocks/test_databricks_maintenance.py` is offline and free. It exercises the
API against a stateful SQL boundary double, including post-commit response loss,
restart/replay, agency isolation, quoted input, refusals, compensation, schema
preflight and compatibility with the existing maintenance writer/kill switch.
It is **not** evidence of live OAuth, remote Databricks MERGE execution, latency
or production readiness. The owner-run steps above are the live acceptance check.

## Connected five-product runtime (A209)

The opt-in profile runs the existing simulated scheduling (8201), telematics
(8202), maintenance (8203), workforce (8204) and rider-alert (8205) APIs as separate
localhost processes. The existing orchestrator/console uses real HTTP clients and
runs at http://127.0.0.1:8212. All use one Databricks warehouse; logical product
schemas do not require separate warehouses. Rider alerts share scheduling's schema.

Run `bash .local/start-connected.sh demo stub` from the project root, or use
`make connected` with DATABRICKS_SERVER_HOSTNAME and DATABRICKS_HTTP_PATH set.
`make connected-openai` selects the existing capped OpenAI reasoner; it is not
required for the free offline tests. A fresh instance name creates a new rehearsal;
reusing a name preserves cloud state and local checkpoints. Ctrl+C stops only the
launcher-owned children. A failed product is restarted individually up to three
times. Existing services on 8103/8112 are not stopped. OAuth is performed once by
the parent and reused through an ignored owner-only `.local/connected/oauth.json`
cache. This file contains credentials: never share or publish it.

The 202 originally imported rows are baseline snapshots. Each named instance
initializes native product documents from those rows into one `api_state` row per
product, under the corresponding schema. `live_records` and `live_*` views expose
current documents as `record_json`, scoped by instance, agency and product. The
baseline `vehicles`, `blocks`, `assignments` and standalone `work_orders` tables
are not updated by the connected runtime. Read the live views for its changes.

An API request loads the cloud snapshot, stages native operations, and atomically
commits effects, sequence values and retry response in the same Delta row. It
checks the revision and commit token before returning success. Outages/conflicts
return 503; there is no local fallback. This is a small-fleet POC with one writer
per product/instance, not a scalable production operational store or distributed
transaction. Native compensation and both approval gates remain in the existing
workflow. Cloud reset/import routes are disabled. Local orchestrator checkpoints,
audit and incident memory remain on the laptop; full UI history/write-log recovery
after replacing the entire console process is not guaranteed.

The original manual 8103 test order was subsequently cancelled through its API;
it remains stored with status `closed` and `cancelled=true`.

### Observed connected acceptance — 2026-10-15

Instance `accept-20261015-a` completed the existing native workflow over five
separate product processes and live Databricks SQL, using stub reasoning and
**owner-authorized scripted acceptance decisions**, not a human review. Observed:
proposal waiting with zero writes; stale approval refused; pause blocks approval;
approved work order, vehicle withdrawal, eight block reassignments, one operator
assignment and eight rider alerts; only scheduling restarted and recovered its
cloud vehicle state while the other process IDs remained unchanged; repair moved
the run to RETURNING and required a separate decision; final DONE, vehicle ready,
eight visible alerts, 20 successful write records and zero invariant violations.
An independent SQL read executed the live views successfully, counted one runtime
work order and eight rider alerts, and confirmed the original baseline counts
still total 202. The earlier standalone manual order was closed/cancelled.
Evidence is ignored under `.local/connected/accept-20261015-a/`.

`UV_NO_SYNC=1 make verify` observed: **1760 passed, 181 skipped in 98.54s**;
ruff clean, **50/50 scenarios passed**, **data-check: OK**.
`UV_NO_SYNC=1 make evals`: **8/8 behavioral evals passed; 0 paid calls**.
Connected tests include all ten existing adapter contract checks, cloud outages,
post-commit response loss/replay, revision conflict, corrupted state, compensation,
rejection, approval recovery and kill switch. Remote latency and a simultaneous
multi-writer deployment are not production-qualified. Live requests consume
Databricks trial compute; the local service processes themselves are not cloud
service deployments. No live OpenAI test was performed.

After acceptance the launcher-owned test services were stopped. Their cloud rows,
local evidence, audit and checkpoints remain available. To rehearse yourself,
choose a fresh instance, for example `bash .local/start-connected.sh learning-1 stub`;
use the console's approval buttons for actual human decisions. In the existing
short-path terminal folder, `bash connect.sh learning-1 stub` invokes the same
launcher. Starting the services and performing cloud requests uses trial compute.
