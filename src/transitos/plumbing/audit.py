"""Append-only audit: raw/resolved event consumers and the DecisionRecord store used by workflow/panel."""
from __future__ import annotations

from transitos.contracts.envelope import TOPIC_RAW, TOPIC_RESOLVED
from transitos.contracts.objects import DecisionRecord

from .bus_sqlite import SqliteBus
from .db import Db
from .kafka_bus import KafkaBus

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_raw(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, bus_offset INTEGER, event_id TEXT NOT NULL UNIQUE,
  type TEXT, agency TEXT, payload TEXT NOT NULL, recorded_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS audit_resolved(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, bus_offset INTEGER, event_id TEXT NOT NULL UNIQUE,
  type TEXT, agency TEXT, payload TEXT NOT NULL, recorded_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS decisions(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, plan_version INTEGER NOT NULL,
  outcome TEXT NOT NULL, payload TEXT NOT NULL, recorded_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS ix_decisions_run ON decisions(run_id, seq);
"""

APPEND_ONLY = "".join(
    f"""
CREATE TRIGGER IF NOT EXISTS {t}_no_update BEFORE UPDATE ON {t} BEGIN SELECT RAISE(ABORT,'{t} is append-only'); END;
CREATE TRIGGER IF NOT EXISTS {t}_no_delete BEFORE DELETE ON {t} BEGIN SELECT RAISE(ABORT,'{t} is append-only'); END;
"""
    for t in ("audit_raw", "audit_resolved", "decisions")
)


# PostgreSQL flavour of the same guarantee: row-level triggers that refuse UPDATE and DELETE.
PG_APPEND_ONLY = ["""
CREATE OR REPLACE FUNCTION transitos_append_only() RETURNS trigger AS $$
BEGIN RAISE EXCEPTION '% is append-only', TG_TABLE_NAME; END $$ LANGUAGE plpgsql"""] + [
    f"CREATE OR REPLACE TRIGGER {t}_append_only BEFORE UPDATE OR DELETE ON {t} "
    "FOR EACH ROW EXECUTE FUNCTION transitos_append_only()"
    for t in ("audit_raw", "audit_resolved", "decisions")]


def ensure_schema(db: Db) -> None:
    if getattr(db, "dialect", "sqlite") == "postgres":
        db.script(SCHEMA)
        with db.lock:
            for stmt in PG_APPEND_ONLY:   # not through script(): the function body contains ';'
                db.conn.execute(stmt)
        return
    db.script(SCHEMA + APPEND_ONLY)


class AuditConsumer:
    """Group `audit-raw` / `audit-resolved`. Idempotent on event id, so replay adds nothing."""

    def __init__(self, db: Db, bus: SqliteBus | KafkaBus, kind: str):
        assert kind in ("raw", "resolved")
        self.db, self.bus, self.kind = db, bus, kind
        self.topic = TOPIC_RAW if kind == "raw" else TOPIC_RESOLVED
        self.group = f"audit-{kind}"
        self.table = f"audit_{kind}"
        ensure_schema(db)

    def run_once(self, limit: int = 500) -> int:
        n, last = 0, None
        with self.db.tx() as c:
            for offset, ev in self.bus.poll(self.topic, self.group, limit):
                c.execute(
                    f"INSERT OR IGNORE INTO {self.table}(bus_offset,event_id,type,agency,payload) VALUES(?,?,?,?,?)",
                    (offset, ev.id, ev.type, ev.agencyid, ev.model_dump_json()))
                last, n = offset, n + 1
        if last is not None:
            self.bus.commit(self.topic, self.group, last)
        return n


class DecisionStore:
    """record_decision appends (never updates); a plan revision or human action is a new row.
    list_for_run returns every record for the run in insertion order (latest last)."""

    def __init__(self, db: Db | str):
        self.db = Db(db) if isinstance(db, str) else db
        ensure_schema(self.db)

    def record_decision(self, rec: DecisionRecord) -> None:
        self.db.execute("INSERT INTO decisions(run_id,plan_version,outcome,payload) VALUES(?,?,?,?)",
                        (rec.run_id, rec.plan_version, rec.outcome, rec.model_dump_json()))

    def list_for_run(self, run_id: str) -> list[DecisionRecord]:
        rows = self.db.execute("SELECT payload FROM decisions WHERE run_id=? ORDER BY seq", (run_id,)).fetchall()
        return [DecisionRecord.model_validate_json(r["payload"]) for r in rows]


def record_decision(db_path: str, rec: DecisionRecord) -> None:
    DecisionStore(db_path).record_decision(rec)


def list_for_run(db_path: str, run_id: str) -> list[DecisionRecord]:
    return DecisionStore(db_path).list_for_run(run_id)
