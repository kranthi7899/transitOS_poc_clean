"""PostgreSQL versions of the stores that live outside plumbing (tool idempotency, kill switch, run index)
and the LangGraph checkpointer, with the same public methods as the SQLite classes they mirror.

The run index reuses `workflow.store.RunsIndex` itself, swapping only its connection for a `PgDb`, so its
SQL stays in one place (translated by `pg.translate`). IdemStore and KillSwitchStore are small enough that
their two queries are repeated here; the shared contract suite (tests/plumbing/test_storage_contract.py)
keeps the two implementations from drifting.
"""
from __future__ import annotations

import threading

from transitos.contracts.objects import DecisionRecord
from transitos.contracts.results import Result
from transitos.tools.idem_store import _TYPES
from transitos.workflow import store as workflow_store
from transitos.workflow.store import RunsIndex

from .audit import DecisionStore
from .pg import PgDb, connect


class PgIdemStore:
    """pg twin of tools.idem_store.IdemStore (first successful result per (agency, tool, key) wins)."""

    def __init__(self, schema: str):
        self.db = PgDb(schema)
        self.db.script(
            "CREATE TABLE IF NOT EXISTS idem(agency TEXT, tool TEXT, key TEXT, result TEXT NOT NULL, "
            "PRIMARY KEY(agency, tool, key))")

    def get(self, agency: str, tool: str, key: str) -> Result | None:
        row = self.db.execute("SELECT result FROM idem WHERE agency=? AND tool=? AND key=?",
                              (agency, tool, key)).fetchone()
        if not row:
            return None
        model = _TYPES[tool]
        rtype = Result[model] if model else Result[dict]  # type: ignore[valid-type]
        return rtype.model_validate_json(row["result"])

    def put(self, agency: str, tool: str, key: str, result: Result) -> None:
        """Only successful results are recorded, so a failed attempt can be retried."""
        if result.status != "ok":
            return
        self.db.execute("INSERT OR IGNORE INTO idem(agency,tool,key,result) VALUES(?,?,?,?)",
                        (agency, tool, key, result.model_dump_json()))

    def close(self) -> None:
        self.db.close()


class PgKillSwitchStore:
    """pg twin of tools.policy.KillSwitchStore: a flag per agency, read live on every write."""

    def __init__(self, schema: str):
        self.db = PgDb(schema)
        self.db.script("CREATE TABLE IF NOT EXISTS kill_switch(agency TEXT PRIMARY KEY, on_flag INTEGER NOT NULL)")

    def set(self, agency: str, on: bool) -> None:
        self.db.execute("INSERT INTO kill_switch(agency,on_flag) VALUES(?,?) "
                        "ON CONFLICT(agency) DO UPDATE SET on_flag=excluded.on_flag", (agency, int(on)))

    def get(self, agency: str) -> bool:
        row = self.db.execute("SELECT on_flag FROM kill_switch WHERE agency=?", (agency,)).fetchone()
        return bool(row["on_flag"]) if row else False

    def close(self) -> None:
        self.db.close()


class PgRunsIndex(RunsIndex):
    """workflow.store.RunsIndex on PostgreSQL (same methods, same SQL; only the connection differs)."""

    def __init__(self, schema: str):   # super().__init__ is deliberately not called: it opens SQLite
        self._db = PgDb(schema)
        self._lock = threading.RLock()
        with self._lock:
            self._db.executescript(workflow_store._SCHEMA)


class PgAuditedRunsIndex(PgRunsIndex):
    """PgRunsIndex that also appends every DecisionRecord revision to the append-only audit store
    (mirror of integration.ports.AuditedRunsIndex)."""

    def __init__(self, schema: str, audit: DecisionStore):
        super().__init__(schema)
        self.audit = audit

    def save_decision(self, run_id: str, plan_version: int, record: dict) -> None:
        super().save_decision(run_id, plan_version, record)
        self.audit.record_decision(DecisionRecord.model_validate(record))


def make_pg_checkpointer(schema: str):
    """LangGraph PostgresSaver (langgraph-checkpoint-postgres) in its own schema. Same use as the SqliteSaver:
    `build_graph(deps, saver)`; `.conn.close()` releases the connection."""
    from langgraph.checkpoint.postgres import PostgresSaver
    saver = PostgresSaver(connect(schema))
    saver.setup()
    return saver
