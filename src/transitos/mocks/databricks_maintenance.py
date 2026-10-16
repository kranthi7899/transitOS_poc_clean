"""Opt-in local SIMULATED maintenance API backed by imported Databricks tables.

This is the first connected product, not a switch for the existing console.
Only read/create/cancel/repair-close are supported; no seed/reset routes exist.
One process and one worker serialize mutations. It is not a multi-writer service.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from collections.abc import Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .clock import SimClock, parse_ts

ASSET_COLUMNS = ("asset", "description", "status", "meter_mi")
PM_COLUMNS = ("asset", "service_code", "due_mi", "meter_mi", "updated_at")
WO_COLUMNS = ("id", "wo_no", "asset", "status", "description", "opened_by",
              "opened_at", "closed_at", "updated_at", "cancelled")


@dataclass(frozen=True)
class Settings:
    hostname: str
    http_path: str
    catalog: str = "workspace"
    schema: str = "maintenance"

    def __post_init__(self):
        if not re.fullmatch(r"[A-Za-z0-9.-]+", self.hostname):
            raise ValueError("DATABRICKS_SERVER_HOSTNAME must be a hostname without https://")
        if not re.fullmatch(r"/sql/1\.0/warehouses/[A-Za-z0-9-]+", self.http_path):
            raise ValueError("DATABRICKS_HTTP_PATH must be the warehouse HTTP path")
        for value in (self.catalog, self.schema):
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
                raise ValueError("Catalog/schema names must contain only letters, numbers and underscores")

    @classmethod
    def from_env(cls):
        try:
            return cls(os.environ["DATABRICKS_SERVER_HOSTNAME"], os.environ["DATABRICKS_HTTP_PATH"],
                       os.getenv("TRANSITOS_DATABRICKS_CATALOG", "workspace"),
                       os.getenv("TRANSITOS_DATABRICKS_SCHEMA", "maintenance"))
        except KeyError as exc:
            raise ValueError(f"Missing connection setting: {exc.args[0]}") from None

    def table(self, name: str) -> str:
        if name not in ("assets", "pm_schedules", "work_orders"):
            raise ValueError("Unsupported maintenance table")
        return f"`{self.catalog}`.`{self.schema}`.`{name}`"


class WarehouseUnavailable(Exception):
    """No credentials or remote SQL details are sent to the HTTP client."""


class Warehouse:
    def __init__(self, settings: Settings, connector: Callable | None = None):
        self.settings, self.connector = settings, connector
        self.connection = None
        self.lock = threading.RLock()

    def query(self, statement: str, parameters=()) -> list[dict]:
        with self.lock:
            try:
                if self.connection is None:
                    connect = self.connector
                    if connect is None:
                        from databricks import sql

                        connect = sql.connect
                    options = {}
                    if os.getenv("TRANSITOS_DATABRICKS_OAUTH_CACHE"):
                        from .databricks_oauth import LocalOAuthCache

                        options["experimental_oauth_persistence"] = LocalOAuthCache(
                            os.environ["TRANSITOS_DATABRICKS_OAUTH_CACHE"])
                    self.connection = connect(
                        server_hostname=self.settings.hostname, http_path=self.settings.http_path,
                        auth_type="databricks-oauth", user_agent_entry="transitos-simulated-maintenance", **options,
                    )
                with self.connection.cursor() as cursor:
                    cursor.execute(statement, parameters=list(parameters))
                    return [row.asDict() for row in cursor.fetchall()] if cursor.description else []
            except Exception:  # noqa: BLE001 — connector failures must not expose credentials or SQL values
                self.close()
                # A write may already have committed. Do not retry it invisibly.
                raise WarehouseUnavailable from None

    def close(self):
        with self.lock:
            if self.connection is not None:
                with suppress(Exception):  # cleanup must not hide the original warehouse failure
                    self.connection.close()
                self.connection = None


class MaintenanceRepository:
    def __init__(self, warehouse: Warehouse, clock: SimClock | None = None):
        self.warehouse, self.clock = warehouse, clock or SimClock()
        self.lock = threading.RLock()

    def preflight(self):
        """Read-only schema check; never auto-create/reset/seed cloud tables."""
        for name, columns in (("assets", ASSET_COLUMNS), ("pm_schedules", PM_COLUMNS),
                              ("work_orders", WO_COLUMNS)):
            self.warehouse.query(
                f"SELECT agency, is_simulated, {', '.join(f'`{c}`' for c in columns)} "
                f"FROM {self.warehouse.settings.table(name)} LIMIT 0"
            )

    def records(self, name: str, agency: str) -> list[dict]:
        columns = {"assets": ASSET_COLUMNS, "pm_schedules": PM_COLUMNS, "work_orders": WO_COLUMNS}[name]
        rows = self.warehouse.query(
            f"SELECT {', '.join(f'`{c}`' for c in columns)} FROM {self.warehouse.settings.table(name)} "
            "WHERE agency = ? AND is_simulated = TRUE", (agency,),
        )
        return sorted(rows, key=lambda row: row.get("id", row.get("asset", "")))

    def one(self, name: str, agency: str, key: str) -> dict:
        # Keep all tenant/simulation filtering inside SQL, including identity lookups.
        column = "id" if name == "work_orders" else "asset"
        matches = [row for row in self.records(name, agency) if row[column] == key]
        if not matches:
            raise HTTPException(404, "work order not found" if name == "work_orders" else "asset not found")
        if len(matches) != 1:
            raise HTTPException(409, "Duplicate product ID; human review required")
        return matches[0]

    def create(self, agency: str, body: WorkOrderRequest, key: str) -> dict:
        # Hash the agency and complete key with explicit framing; no raw key in product rows.
        digest = hashlib.sha256(json.dumps([agency, key]).encode()).hexdigest()
        wo_id = f"wo-{digest}"
        with self.lock:
            existing = [row for row in self.records("work_orders", agency) if row["id"] == wo_id]
            if existing:
                return self._same_request(existing, body)
            asset = self.one("assets", agency, body.asset)
            if asset["status"] == "retired":
                raise HTTPException(409, "asset is retired: no new work orders")
            now = self.clock.iso()
            row = {"id": wo_id, "wo_no": f"WO-{digest.upper()}", "asset": body.asset, "status": "open",
                   "description": body.description, "opened_by": body.opened_by,
                   "opened_at": now, "closed_at": None, "updated_at": now, "cancelled": False}
            columns = ("agency", "is_simulated", *WO_COLUMNS)
            # Aliases are inside SELECT, not attached to the MERGE source table.
            source = ", ".join(f"CAST(? AS {'BOOLEAN' if c in ('is_simulated', 'cancelled') else 'STRING'}) AS `{c}`"
                               for c in columns)
            target = self.warehouse.settings.table("work_orders")
            self.warehouse.query(
                f"MERGE INTO {target} AS target USING (SELECT {source}) AS source "
                "ON target.agency = source.agency AND target.id = source.id "
                "WHEN NOT MATCHED THEN INSERT *",
                (agency, True, *(row[c] for c in WO_COLUMNS)),
            )
            # Read-back is authoritative, including after an earlier ambiguous response/restart.
            return self._same_request([self.one("work_orders", agency, wo_id)], body)

    @staticmethod
    def _same_request(rows: list[dict], body: WorkOrderRequest) -> dict:
        if len(rows) != 1:
            raise HTTPException(409, "Duplicate work order ID; human review required")
        row = rows[0]
        if any(row[field] != getattr(body, field) for field in ("asset", "description", "opened_by")):
            raise HTTPException(409, "Idempotency-Key already used for a different request")
        return row

    def close(self, agency: str, wo_id: str, *, cancel: bool = False) -> dict:
        with self.lock:
            row = self.one("work_orders", agency, wo_id)
            if cancel and row["opened_by"] != "system":
                raise HTTPException(409, "only system-opened work orders can be cancelled")
            if row["status"].lower() == "closed":
                return row
            now = self.clock.iso()
            self.warehouse.query(
                f"UPDATE {self.warehouse.settings.table('work_orders')} "
                "SET status = ?, closed_at = ?, updated_at = ?, cancelled = ? "
                "WHERE agency = ? AND id = ? AND is_simulated = TRUE AND lower(status) <> 'closed'",
                ("closed", now, now, cancel, agency, wo_id),
            )
            updated = self.one("work_orders", agency, wo_id)
            if updated["status"].lower() != "closed":
                raise WarehouseUnavailable
            return updated


class WorkOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=4000)
    opened_by: Literal["human", "system"] = "human"


def create_app(repository: MaintenanceRepository | None = None) -> FastAPI:
    repo = repository or MaintenanceRepository(Warehouse(Settings.from_env()))

    @asynccontextmanager
    async def lifespan(app):
        yield
        repo.warehouse.close()

    app = FastAPI(title="SIMULATED maintenance — Databricks", lifespan=lifespan,
                  description="Local API proof of connection. Orchestrator approval remains upstream. "
                  "Fixed simulation clock; one process/worker. No seed/reset or pending-repair routes.")

    @app.exception_handler(WarehouseUnavailable)
    async def unavailable(request, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=503, content={"detail":
            "Databricks unavailable; a write may have committed. Retry with the same Idempotency-Key. "
            "No local fallback was used."})

    def tenant(x_agency: str | None = Header(default=None)) -> str:
        if not x_agency or len(x_agency) > 128:
            raise HTTPException(400, "X-Agency header required (maximum 128 characters)")
        return x_agency

    @app.get("/health")
    def health():
        return {"simulated": True, "storage": "databricks", "database_checked": False,
                "simulation_time": repo.clock.iso(), "orchestrator_connected": False}

    @app.get("/assets")
    def assets(agency: str = Depends(tenant)):
        return repo.records("assets", agency)

    @app.get("/assets/{asset}")
    def asset(asset: str, agency: str = Depends(tenant)):
        return repo.one("assets", agency, asset)

    @app.get("/pm-schedules")
    def schedules(updated_since: str | None = None, agency: str = Depends(tenant)):
        rows = repo.records("pm_schedules", agency)
        return _since(rows, updated_since)

    @app.get("/workorders")
    def workorders(asset: str | None = None, status: str | None = None, updated_since: str | None = None,
                   agency: str = Depends(tenant)):
        rows = repo.records("work_orders", agency)
        return _since([row for row in rows if (asset is None or row["asset"] == asset)
                       and (status is None or row["status"] == status)], updated_since)

    @app.get("/workorders/{wo_id}")
    def workorder(wo_id: str, agency: str = Depends(tenant)):
        return repo.one("work_orders", agency, wo_id)

    @app.post("/workorders")
    def create(body: WorkOrderRequest, idempotency_key: str | None = Header(default=None),
               agency: str = Depends(tenant)):
        if not idempotency_key or len(idempotency_key) > 512:
            raise HTTPException(400, "Idempotency-Key required (maximum 512 characters)")
        return repo.create(agency, body, idempotency_key)

    @app.post("/workorders/{wo_id}/cancel")
    def cancel(wo_id: str, agency: str = Depends(tenant)):
        return repo.close(agency, wo_id, cancel=True)

    @app.post("/sim/workorders/{wo_id}/close")
    def repair_close(wo_id: str, agency: str = Depends(tenant)):
        return repo.close(agency, wo_id)

    return app


def _since(rows: list[dict], timestamp: str | None) -> list[dict]:
    if timestamp is None:
        return rows
    try:
        since = parse_ts(timestamp)
    except ValueError:
        raise HTTPException(422, "updated_since must be an ISO timestamp") from None
    return [row for row in rows if parse_ts(row["updated_at"]) >= since]


def main():
    import uvicorn

    repo = MaintenanceRepository(Warehouse(Settings.from_env()))
    print("SIMULATED maintenance: checking Databricks schema (may start warehouse/use trial credits).")
    print("Complete browser sign-in if prompted. No model calls; no records created at startup.")
    try:
        repo.preflight()
    except WarehouseUnavailable:
        raise SystemExit("Databricks schema check failed. Check sign-in, connection settings and the three "
                         "imported maintenance tables. No local fallback was used.") from None
    print(f"Ready at http://127.0.0.1:8103/docs — fixed simulation time {repo.clock.iso()}")
    try:
        uvicorn.run(create_app(repo), host="127.0.0.1", port=8103, workers=1, log_level="warning")
    finally:
        repo.warehouse.close()


if __name__ == "__main__":
    main()
