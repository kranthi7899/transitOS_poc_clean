"""Export fresh SIMULATED product snapshots as owner-run Databricks SQL.

Offline only: no Databricks credentials, network connections or model calls.
Run from the project root with .venv/bin/python scripts/export_databricks_seed.py.
"""
from __future__ import annotations

import argparse
import json
import re
from contextlib import ExitStack
from pathlib import Path

from fastapi.testclient import TestClient

from transitos.mocks import maintenance, scheduling, telematics, workforce
from transitos.mocks.chaos import Chaos
from transitos.mocks.clock import DEFAULT_START, SimClock

PRODUCTS = {
    "telematics": (telematics, {"vehicles": ("/vehicles", "id")}),
    "maintenance": (maintenance, {
        "assets": ("/assets", "asset"), "pm_schedules": ("/pm-schedules", "asset"),
    }),
    "scheduling": (scheduling, {
        "vehicles": ("/vehicles", "id"), "blocks": ("/blocks", "id"),
    }),
    "workforce": (workforce, {}),
}


def flatten(row: dict, prefix: str = "") -> dict:
    """Flatten nested product fields; missing/null values stay missing/null."""
    result = {}
    for key, value in row.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            result.update(flatten(value, f"{name}_"))
        else:
            result[name] = json.dumps(value) if isinstance(value, list) else value
    return result


def identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError(f"Unsupported SQL identifier: {value!r}")
    return f"`{value}`"


def dtype(values: list) -> str:
    known = [value for value in values if value is not None]
    if known and all(isinstance(value, bool) for value in known):
        return "BOOLEAN"
    if known and all(type(value) is int for value in known):
        return "BIGINT"
    if known and all(type(value) in (int, float) for value in known):
        return "DOUBLE"
    return "STRING"


def literal(value, sql_type: str) -> str:
    if value is None:
        return f"CAST(NULL AS {sql_type})"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return f"CAST({value} AS {sql_type})"
    # Databricks regular string literals interpret backslash escapes.
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


def table_sql(catalog: str, product: str, table: str, rows: list[dict], key: str) -> str:
    columns = list(dict.fromkeys(column for row in rows for column in row))
    types = {column: dtype([row.get(column) for row in rows]) for column in columns}
    target = ".".join(map(identifier, (catalog, product, table)))
    declarations = ",\n  ".join(f"{identifier(c)} {types[c]}" for c in columns)
    names = ", ".join(map(identifier, columns))
    values = ",\n  ".join(
        "(" + ", ".join(literal(row.get(c), types[c]) for c in columns) + ")" for row in rows
    )
    return (
        f"-- Expected seed records: {len(rows)}. Re-running inserts missing keys only.\n"
        f"CREATE TABLE IF NOT EXISTS {target} (\n  {declarations}\n) USING DELTA\n"
        "COMMENT 'SIMULATED TransitOS baseline; offline snapshot, not live product integration';\n\n"
        f"MERGE INTO {target} AS target\nUSING (\n  SELECT * FROM VALUES\n  {values}\n"
        f"  AS seed ({names})\n) AS source\n"
        f"ON target.agency = source.agency AND target.{identifier(key)} = source.{identifier(key)}\n"
        "WHEN NOT MATCHED THEN INSERT *;\n\n"
        f"SELECT '{product}.{table}' AS imported_table, COUNT(*) AS seed_rows\n"
        f"FROM {target} WHERE agency = 'agency-042' AND is_simulated = TRUE;\n"
    )


def collect() -> dict:
    """Read isolated fresh mocks; never touch the owner's running demo or databases."""
    clock = SimClock(DEFAULT_START)
    headers = {"X-Agency": "agency-042"}
    data = {}
    with ExitStack() as stack:
        clients = {
            product: stack.enter_context(TestClient(module.create_app(clock, Chaos())))
            for product, (module, _) in PRODUCTS.items()
        }
        for client in clients.values():
            client.post("/sim/seed", headers=headers).raise_for_status()

        def read(product: str, path: str):
            response = clients[product].get(path, headers=headers)
            response.raise_for_status()
            return response.json()

        def enriched(rows: list[dict], path: str) -> list[dict]:
            return [{"agency": "agency-042", "is_simulated": True,
                     "snapshot_at": clock.iso(), "source_endpoint": path, **flatten(row)} for row in rows]

        for product, (_, tables) in PRODUCTS.items():
            data[product] = {
                table: {"key": key, "rows": enriched(read(product, path), path)}
                for table, (path, key) in tables.items()
            }
        # Read typed JSON operator records, retaining null fields instead of CSV strings.
        from transitos.mocks.seed import build_fleet

        operators = build_fleet("agency-042", clock.now()).operators
        data["workforce"] = {
            "operators": {"key": "operator", "rows": enriched([
                read("workforce", f"/operators/{op['native']}") for op in operators
            ], "/operators/{operator}")},
            "assignments": {"key": "block", "rows": enriched([
                read("workforce", f"/assignments/{block['id']}")
                for block in data["scheduling"]["blocks"]["rows"]
            ], "/assignments/{block}")},
        }
        # Baseline must not manufacture incidents or maintenance actions.
        assert read("telematics", "/faults/current") == []
        assert read("maintenance", "/workorders") == []
    return data


def validate(data: dict) -> None:
    blocks = data["scheduling"]["blocks"]["rows"]
    vehicles = {row["id"] for row in data["scheduling"]["vehicles"]["rows"]}
    operators = {row["operator"] for row in data["workforce"]["operators"]["rows"]}
    assets = {row["asset"] for row in data["maintenance"]["assets"]["rows"]}
    assert all(row["vehicle"] in vehicles and row["operator"] in operators for row in blocks)
    assert all(row["asset"] in assets for row in data["maintenance"]["pm_schedules"]["rows"])
    assignments = {row["block"]: row["operator"] for row in data["workforce"]["assignments"]["rows"]}
    assert all(assignments[row["id"]] == row["operator"] for row in blocks)
    for tables in data.values():
        for table in tables.values():
            keys = [(row["agency"], row[table["key"]]) for row in table["rows"]]
            assert len(keys) == len(set(keys)), "Duplicate seed key"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", default="workspace")
    parser.add_argument("--output", type=Path, default=Path(".local/databricks-seed"))
    args = parser.parse_args()
    identifier(args.catalog)
    data = collect()
    validate(data)
    args.output.mkdir(parents=True, exist_ok=True)
    counts = {}
    for number, (product, tables) in enumerate(data.items(), 1):
        sql = (
            "-- SIMULATED products only. Fixed simulation day: 2026-01-05 UTC.\n"
            "-- Import snapshot only; this does not connect the runtime to Databricks.\n"
            "-- Existing records with matching agency/native IDs are never overwritten.\n\n"
            f"CREATE SCHEMA IF NOT EXISTS {identifier(args.catalog)}.{identifier(product)};\n\n"
        )
        for table, content in tables.items():
            sql += table_sql(args.catalog, product, table, content["rows"], content["key"]) + "\n"
            counts[f"{product}.{table}"] = len(content["rows"])
        (args.output / f"{number:02d}_{product}.sql").write_text(sql)
    count_queries = []
    for table, count in counts.items():
        product, name = table.split(".")
        target = ".".join(map(identifier, (args.catalog, product, name)))
        count_queries.append(
            f"SELECT '{table}' AS table_name, {count} AS expected_rows, COUNT(*) AS actual_rows "
            f"FROM {target} WHERE agency = 'agency-042' AND is_simulated = TRUE"
        )
    (args.output / "05_verify_counts.sql").write_text(
        "-- Run after all four imports. Expected and actual counts should match.\n"
        + "\nUNION ALL\n".join(count_queries) + ";\n"
    )
    (args.output / "snapshot.json").write_text(json.dumps(data, indent=2) + "\n")
    (args.output / "manifest.json").write_text(json.dumps({
        "simulated": True, "agency": "agency-042", "snapshot_at": DEFAULT_START.isoformat(),
        "catalog": args.catalog, "expected_seed_rows": counts,
        "active_faults": 0, "work_orders": 0, "runtime_connected": False,
    }, indent=2) + "\n")
    print(json.dumps(counts, indent=2))
    print(f"Validated {sum(counts.values())} records; owner-run SQL written to {args.output}")


if __name__ == "__main__":
    main()
