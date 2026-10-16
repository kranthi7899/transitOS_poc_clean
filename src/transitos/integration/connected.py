"""Opt-in connected profile: separate localhost product processes, Databricks persistence.

Original imported tables are immutable baselines. Runtime instances use api_state
and live_* views in the same four schemas; alerts share scheduling's schema.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx
from fastapi import Header

from transitos.mocks import alerts, maintenance, scheduling, telematics, workforce
from transitos.mocks.chaos import Chaos
from transitos.mocks.clock import DEFAULT_START, SimClock
from transitos.mocks.databricks_maintenance import Settings, Warehouse
from transitos.mocks.databricks_state import (
    PRODUCT_SCHEMAS,
    AtomicProductMiddleware,
    ProductState,
    encode,
    state_table,
)
from transitos.mocks.seed import build_fleet

AGENCY = "agency-042"
MODULES = {"scheduling": scheduling, "telematics": telematics, "maintenance": maintenance,
           "workforce": workforce, "alerts": alerts}
PORTS = {"scheduling": 8201, "telematics": 8202, "maintenance": 8203, "workforce": 8204, "alerts": 8205}
BASE = Path(".local/connected")
MODULE = "transitos.integration.connected"
SOURCES = {"telematics": {"vehicles": ("vehicles", "id")},
           "maintenance": {"assets": ("assets", "asset"), "pm": ("pm_schedules", "asset")},
           "scheduling": {"vehicles": ("vehicles", "id"), "blocks": ("blocks", "id")},
           "workforce": {"operators": ("operators", "operator"), "assignments": ("assignments", "block")},
           "alerts": {}}


def baseline(warehouse: Warehouse, product: str) -> dict:
    """Use imported native facts, not a newly invented fleet or current local memory."""
    docs = {}
    schema = PRODUCT_SCHEMAS[product]
    for coll, (table, key) in SOURCES[product].items():
        rows = warehouse.query(f"SELECT * FROM `{warehouse.settings.catalog}`.`{schema}`.`{table}` "
                               "WHERE agency = ? AND is_simulated = TRUE", (AGENCY,))
        native = []
        for row in rows:
            item = {k: v for k, v in row.items() if k not in
                    ("agency", "is_simulated", "snapshot_at", "source_endpoint")}
            if coll == "blocks":
                needs = {k[6:]: item.pop(k) for k in list(item) if k.startswith("needs_")}
                item["needs"] = needs
            native.append(item)
        if not native or len({row[key] for row in native}) != len(native):
            raise ValueError(f"Empty/duplicate baseline collection: {product}.{table}")
        docs[coll] = {row[key]: row for row in native}
    fleet = build_fleet(AGENCY, DEFAULT_START)
    expected = {
        "telematics": {"vehicles": {v["tele"] for v in fleet.vehicles}},
        "maintenance": {"assets": {v["maint"] for v in fleet.vehicles}, "pm": {v["maint"] for v in fleet.vehicles}},
        "scheduling": {"vehicles": {v["sched"] for v in fleet.vehicles}, "blocks": {b["id"] for b in fleet.blocks}},
        "workforce": {"operators": {o["native"] for o in fleet.operators},
                      "assignments": {b["id"] for b in fleet.blocks}}, "alerts": {},
    }[product]
    if any(set(docs[coll]) != ids for coll, ids in expected.items()):
        raise ValueError(f"Baseline IDs differ from existing crosswalk: {product}; human review required")
    return docs


def bootstrap(warehouse: Warehouse, instance: str):
    """Add isolated state and inspection views. Never reset/delete existing data."""
    # Validate identifiers before any SQL mutation.
    ProductState(warehouse, "maintenance", instance, AGENCY, SimClock())
    catalog = warehouse.settings.catalog
    for schema in sorted(set(PRODUCT_SCHEMAS.values())):
        table = f"`{catalog}`.`{schema}`.`api_state`"
        warehouse.query(f"CREATE TABLE IF NOT EXISTS {table} (instance_id STRING, agency STRING, product STRING, "
                        "is_simulated BOOLEAN, body STRING, revision BIGINT, clock_at STRING, commit_token STRING) "
                        "USING DELTA COMMENT 'SIMULATED product API state; one row per product instance'")
        warehouse.query(f"CREATE VIEW IF NOT EXISTS `{catalog}`.`{schema}`.`live_records` AS "
                        "SELECT instance_id, agency, product, revision, collection, record_id, record_json "
                        f"FROM (SELECT * FROM {table} WHERE is_simulated = TRUE) states "
                        "LATERAL VIEW explode(from_json(body, 'MAP<STRING,MAP<STRING,STRING>>')) "
                        "collections AS collection, records LATERAL VIEW explode(records) documents AS record_id, record_json")
    for product in MODULES:
        docs, table = baseline(warehouse, product), state_table(warehouse.settings, product)
        warehouse.query(f"MERGE INTO {table} AS target USING (SELECT CAST(? AS STRING) AS instance_id, "
                        "CAST(? AS STRING) AS agency, CAST(? AS STRING) AS product, TRUE AS is_simulated, "
                        "CAST(? AS STRING) AS body, CAST(0 AS BIGINT) AS revision, CAST(? AS STRING) AS clock_at, "
                        "'bootstrap' AS commit_token) AS source ON target.instance_id = source.instance_id "
                        "AND target.agency = source.agency AND target.product = source.product "
                        "WHEN NOT MATCHED THEN INSERT *", (instance, AGENCY, product, encode(docs), SimClock().iso()))
        state = ProductState(warehouse, product, instance, AGENCY, SimClock())
        state.load()  # refuses duplicate/missing state rows
        for coll in (*SOURCES[product], *(('wo',) if product == "maintenance" else ()),
                     *(('faults', 'events') if product == "telematics" else ()),
                     *(('alerts',) if product == "alerts" else ())):
            name = {"wo": "work_orders", "pm": "pm_schedules"}.get(coll, coll)
            schema = PRODUCT_SCHEMAS[product]
            prefix = "rider_" if product == "alerts" else ""
            warehouse.query(f"CREATE VIEW IF NOT EXISTS `{catalog}`.`{schema}`.`live_{prefix}{name}` AS "
                            "SELECT *, get_json_object(record_json, '$.status') AS status, "
                            "get_json_object(record_json, '$.asset') AS asset, "
                            "get_json_object(record_json, '$.vehicle') AS vehicle, "
                            "get_json_object(record_json, '$.operator') AS operator "
                            f"FROM `{catalog}`.`{schema}`.`live_records` WHERE product = '{product}' "
                            f"AND collection = '{coll}'")


def product_app(product: str, instance: str, warehouse: Warehouse):
    clock = SimClock()
    store = ProductState(warehouse, product, instance, AGENCY, clock)
    app = MODULES[product].create_app(clock, Chaos(), store)

    @app.get("/health")
    def health():
        return {"simulated": True, "product": product, "instance": instance, "storage": "databricks",
                "schema": PRODUCT_SCHEMAS[product], "database_checked": False}

    @app.get("/sim/records/{collection}")
    def records(collection: str, x_agency: str = Header()):
        return store.all(x_agency, collection)

    app.add_middleware(AtomicProductMiddleware, store=store)
    app.state.cloud_store = store
    return app


def launch(instance: str, reasoner: str):
    """Parent owns only its child processes; existing owner services are untouched."""
    import socket

    for port in (*PORTS.values(), 8212):
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                raise SystemExit(f"Port {port} is occupied. Stop that connected profile before relaunching.")
    os.environ.setdefault("TRANSITOS_DATABRICKS_OAUTH_CACHE", str((BASE / "oauth.json").resolve()))
    warehouse = Warehouse(Settings.from_env())
    try:
        bootstrap(warehouse, instance)
    finally:
        warehouse.close()
    root = BASE / instance
    root.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "TRANSITOS_CONNECTED_INSTANCE": instance, "TRANSITOS_REASONER": reasoner}
    children, logs = [], []
    restarts = {product: 0 for product in MODULES}
    try:
        for product in MODULES:
            log = (root / f"{product}.log").open("a")
            logs.append(log)
            children.append(subprocess.Popen([sys.executable, "-m", MODULE, "product", "--product", product,
                                              "--instance", instance], env=env, stdout=log, stderr=log))
        with httpx.Client(timeout=3) as client:
            for product, port in PORTS.items():
                deadline = time.monotonic() + 60
                while True:
                    try:
                        response = client.get(f"http://127.0.0.1:{port}/health")
                        response.raise_for_status()
                        if response.json()["instance"] != instance:
                            raise RuntimeError("Instance mismatch")
                        break
                    except httpx.HTTPError:
                        if time.monotonic() > deadline or any(child.poll() is not None for child in children):
                            raise RuntimeError(f"{product} did not start; see {root}/{product}.log") from None
                        time.sleep(.2)
        log = (root / "console.log").open("a")
        logs.append(log)
        children.append(subprocess.Popen([sys.executable, "-m", MODULE, "console", "--instance", instance],
                                          env=env, stdout=log, stderr=log))
        print(f"Connected SIMULATED products — instance {instance}; reasoning {reasoner}", flush=True)
        print("Console: http://127.0.0.1:8212 · product APIs: ports 8201–8205", flush=True)
        print("Databricks trial compute is used by product API requests. Ctrl+C stops these children only.", flush=True)
        def manifest():
            target = root / "pids.json"
            temp = target.with_suffix(".tmp")
            temp.write_text(json.dumps({"instance": instance, "parent": os.getpid(),
                "products": {product: child.pid for product, child in zip(MODULES, children)},
                "console": children[-1].pid, "restarts": restarts}, indent=2))
            temp.replace(target)
        manifest()
        while children[-1].poll() is None:
            for index, product in enumerate(MODULES):
                if children[index].poll() is not None:
                    restarts[product] += 1
                    if restarts[product] > 3:
                        raise RuntimeError(f"{product} failed repeatedly; inspect its log")
                    print(f"Restarting only {product}; cloud state is preserved", flush=True)
                    children[index] = subprocess.Popen([sys.executable, "-m", MODULE, "product", "--product", product,
                        "--instance", instance], env=env, stdout=logs[index], stderr=logs[index])
                    manifest()
            time.sleep(.5)
        raise RuntimeError(f"Connected console stopped; inspect {root}/console.log")
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        for log in logs:
            log.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("launch", "product", "console", "bootstrap"))
    parser.add_argument("--instance", default=os.getenv("TRANSITOS_CONNECTED_INSTANCE", "demo"))
    parser.add_argument("--product", choices=list(MODULES))
    parser.add_argument("--reasoner", choices=("stub", "openai"), default="stub")
    args = parser.parse_args()
    if args.mode == "launch":
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        launch(args.instance, args.reasoner)
    elif args.mode == "console":
        import uvicorn

        from .connected_world import connected_console

        uvicorn.run(connected_console(args.instance), host="127.0.0.1", port=8212, log_level="warning")
    else:
        warehouse = Warehouse(Settings.from_env())
        try:
            if args.mode == "bootstrap":
                bootstrap(warehouse, args.instance)
            elif args.product:
                import uvicorn

                uvicorn.run(product_app(args.product, args.instance, warehouse), host="127.0.0.1",
                            port=PORTS[args.product], workers=1, log_level="warning")
            else:
                parser.error("product mode requires --product")
        finally:
            warehouse.close()


if __name__ == "__main__":
    main()
