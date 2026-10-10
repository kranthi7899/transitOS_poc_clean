"""SQLite idempotency store for products with no idempotency keys (scheduling, workforce, alerts).
A repeated (agency, tool, key) returns the prior stored Result without calling the product."""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from pydantic import BaseModel

from transitos.contracts.objects import Block, Vehicle, WorkOrder
from transitos.contracts.results import Result

_TYPES: dict[str, type[BaseModel] | None] = {
    "create_work_order": WorkOrder, "cancel_work_order": WorkOrder,
    "set_vehicle_unavailable": Vehicle, "set_vehicle_available": Vehicle,
    "reassign_block": Block, "assign_operator": Block, "publish_rider_alert": None,  # dict
}


class IdemStore:
    def __init__(self, db_path: str | Path = ":memory:"):
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=5)
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS idem(agency TEXT, tool TEXT, key TEXT, result TEXT NOT NULL, "
                "PRIMARY KEY(agency, tool, key))")

    def get(self, agency: str, tool: str, key: str) -> Result | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT result FROM idem WHERE agency=? AND tool=? AND key=?", (agency, tool, key)).fetchone()
        if not row:
            return None
        model = _TYPES[tool]
        rtype = Result[model] if model else Result[dict]  # type: ignore[valid-type]
        return rtype.model_validate_json(row[0])

    def put(self, agency: str, tool: str, key: str, result: Result) -> None:
        """Only successful results are recorded, so a failed attempt can be retried."""
        if result.status != "ok":
            return
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO idem(agency,tool,key,result) VALUES(?,?,?,?)",
                (agency, tool, key, result.model_dump_json()))
