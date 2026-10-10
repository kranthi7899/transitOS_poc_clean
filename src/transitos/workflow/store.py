"""SQLite run index + persisted plans / decision records / escalations (workflow-owned tables).
Separate from the LangGraph checkpointer (which owns graph state); this is what the starter,
timers and the panel query. One ACTIVE run per (agency, vehicle) is enforced by a unique index (A9)."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from typing import Any

TERMINAL = {"DONE", "CLOSED_REJECTED", "ESCALATED"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events_seen(event_id TEXT PRIMARY KEY, run_id TEXT, action TEXT);
CREATE TABLE IF NOT EXISTS runs(
  run_id TEXT PRIMARY KEY, agency TEXT, vehicle TEXT, trigger_event_id TEXT,
  status TEXT, active INTEGER DEFAULT 1, awaiting_since TEXT, deadline TEXT,
  stale INTEGER DEFAULT 0, pending_resume TEXT, context TEXT DEFAULT '[]');
CREATE UNIQUE INDEX IF NOT EXISTS one_active_per_vehicle ON runs(agency, vehicle) WHERE active=1;
CREATE TABLE IF NOT EXISTS plans(run_id TEXT, plan_version INTEGER, plan TEXT, PRIMARY KEY(run_id, plan_version));
CREATE TABLE IF NOT EXISTS decisions(run_id TEXT, plan_version INTEGER, record TEXT, PRIMARY KEY(run_id, plan_version));
CREATE TABLE IF NOT EXISTS escalations(run_id TEXT PRIMARY KEY, detail TEXT);
"""


class RunsIndex:
    def __init__(self, path: str = ":memory:"):
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.executescript(_SCHEMA)

    # -- helpers
    def _q(self, sql: str, *args) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def _x(self, sql: str, *args) -> int:
        with self._lock:
            return self._db.execute(sql, args).rowcount

    # -- event dedupe
    def seen(self, event_id: str) -> bool:
        return bool(self._q("SELECT 1 FROM events_seen WHERE event_id=?", event_id))

    def mark_seen(self, event_id: str, run_id: str | None, action: str) -> None:
        self._x("INSERT OR IGNORE INTO events_seen VALUES(?,?,?)", event_id, run_id, action)

    def seen_events(self, action: str | None = None) -> list[dict]:
        """Rows of the event-dedupe table {event_id, run_id, action}, optionally one action (e.g. 'attached')."""
        sql = "SELECT * FROM events_seen" + (" WHERE action=?" if action else "") + " ORDER BY rowid"
        return [dict(r) for r in self._q(sql, *([action] if action else []))]

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # -- runs
    def claim(self, agency: str, vehicle: str, run_id: str, trigger_event_id: str) -> str:
        """Return the active run id for the vehicle (== run_id if we just claimed it)."""
        with self._lock:
            row = self._q("SELECT run_id FROM runs WHERE agency=? AND vehicle=? AND active=1", agency, vehicle)
            if row:
                return row[0]["run_id"]
            self._x("INSERT OR IGNORE INTO runs(run_id,agency,vehicle,trigger_event_id,status) VALUES(?,?,?,?,?)",
                    run_id, agency, vehicle, trigger_event_id, "STARTED")
            return run_id

    def get_run(self, run_id: str) -> dict | None:
        r = self._q("SELECT * FROM runs WHERE run_id=?", run_id)
        return dict(r[0]) if r else None

    def active_run(self, agency: str, vehicle: str) -> dict | None:
        r = self._q("SELECT * FROM runs WHERE agency=? AND vehicle=? AND active=1", agency, vehicle)
        return dict(r[0]) if r else None

    def set_status(self, run_id: str, status: str) -> None:
        self._x("UPDATE runs SET status=? WHERE run_id=?", status, run_id)

    def release(self, run_id: str, status: str) -> None:
        self._x("UPDATE runs SET status=?, active=0, awaiting_since=NULL, deadline=NULL WHERE run_id=?",
                status, run_id)

    def set_awaiting(self, run_id: str, since: datetime, deadline: datetime) -> None:
        self._x("UPDATE runs SET awaiting_since=?, deadline=? WHERE run_id=?",
                since.isoformat(), deadline.isoformat(), run_id)

    def awaiting_runs(self, agency: str | None = None) -> list[dict]:
        sql = "SELECT * FROM runs WHERE active=1 AND status='AWAITING_APPROVAL'"
        rows = self._q(sql + (" AND agency=?" if agency else ""), *([agency] if agency else []))
        return [dict(r) for r in rows]

    def list_runs(self) -> list[dict]:
        return [dict(r) for r in self._q("SELECT * FROM runs ORDER BY rowid")]

    # -- context / signals
    def add_context(self, run_id: str, event: dict) -> None:
        with self._lock:
            r = self.get_run(run_id)
            if not r:
                return
            ctx = json.loads(r["context"] or "[]")
            if any(e.get("id") == event.get("id") for e in ctx):
                return
            ctx.append(event)
            self._x("UPDATE runs SET context=? WHERE run_id=?", json.dumps(ctx, default=str), run_id)

    def context(self, run_id: str) -> list[dict]:
        r = self.get_run(run_id)
        return json.loads(r["context"]) if r else []

    def mark_stale(self, run_id: str) -> None:
        self._x("UPDATE runs SET stale=1 WHERE run_id=?", run_id)

    def pop_stale(self, run_id: str) -> bool:
        with self._lock:
            r = self.get_run(run_id)
            self._x("UPDATE runs SET stale=0 WHERE run_id=?", run_id)
            return bool(r and r["stale"])

    def set_pending_resume(self, run_id: str, payload: dict) -> None:
        self._x("UPDATE runs SET pending_resume=? WHERE run_id=?", json.dumps(payload, default=str), run_id)

    def pop_pending_resume(self, run_id: str) -> dict | None:
        with self._lock:
            r = self.get_run(run_id)
            if not r or not r["pending_resume"]:
                return None
            self._x("UPDATE runs SET pending_resume=NULL WHERE run_id=?", run_id)
            return json.loads(r["pending_resume"])

    # -- plans / decisions / escalations
    def save_plan(self, run_id: str, plan_version: int, plan: dict) -> None:
        self._x("INSERT OR REPLACE INTO plans VALUES(?,?,?)", run_id, plan_version, json.dumps(plan, default=str))

    def get_plan(self, run_id: str, plan_version: int) -> dict | None:
        r = self._q("SELECT plan FROM plans WHERE run_id=? AND plan_version=?", run_id, plan_version)
        return json.loads(r[0]["plan"]) if r else None

    def save_decision(self, run_id: str, plan_version: int, record: dict) -> None:
        self._x("INSERT OR REPLACE INTO decisions VALUES(?,?,?)", run_id, plan_version,
                json.dumps(record, default=str))

    def get_decision(self, run_id: str, plan_version: int) -> dict | None:
        r = self._q("SELECT record FROM decisions WHERE run_id=? AND plan_version=?", run_id, plan_version)
        return json.loads(r[0]["record"]) if r else None

    def save_escalation(self, run_id: str, detail: dict[str, Any]) -> None:
        self._x("INSERT OR REPLACE INTO escalations VALUES(?,?)", run_id, json.dumps(detail, default=str))

    def get_escalation(self, run_id: str) -> dict | None:
        r = self._q("SELECT detail FROM escalations WHERE run_id=?", run_id)
        return json.loads(r[0]["detail"]) if r else None
