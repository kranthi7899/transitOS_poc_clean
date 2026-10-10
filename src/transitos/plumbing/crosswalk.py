"""SQLite implementation of contracts.crosswalk.Crosswalk."""
from __future__ import annotations

import sqlite3

from transitos.contracts.crosswalk import CrosswalkEntry
from transitos.contracts.envelope import CloudEvent

from .db import Db

SCHEMA = """
CREATE TABLE IF NOT EXISTS xw_entries(
  agency TEXT NOT NULL, product TEXT NOT NULL, native_id TEXT NOT NULL,
  entity TEXT NOT NULL, canonical_id TEXT NOT NULL,
  PRIMARY KEY(agency, product, native_id));
CREATE INDEX IF NOT EXISTS ix_xw_canon ON xw_entries(agency, canonical_id);
CREATE TABLE IF NOT EXISTS xw_unresolved(
  agency TEXT NOT NULL, product TEXT NOT NULL, native_id TEXT NOT NULL,
  event_id TEXT NOT NULL, payload TEXT,
  PRIMARY KEY(agency, product, native_id, event_id));
"""


class SqliteCrosswalk:
    def __init__(self, db: Db | str, bus=None):
        """bus (optional, a SqliteBus) lets resolve() recover a parked event body from events.raw when
        add_unresolved() was called without it (the contract signature carries only the event id)."""
        self.db = Db(db) if isinstance(db, str) else db
        self.bus = bus
        self.db.script(SCHEMA)

    def lookup(self, agency: str, product: str, native_id: str) -> str | None:
        r = self.db.execute(
            "SELECT canonical_id FROM xw_entries WHERE agency=? AND product=? AND native_id=?",
            (agency, product, native_id)).fetchone()
        return r["canonical_id"] if r else None

    def all_ids(self, agency: str, canonical_id: str) -> dict[str, str]:
        rows = self.db.execute(
            "SELECT product,native_id FROM xw_entries WHERE agency=? AND canonical_id=?", (agency, canonical_id)
        ).fetchall()
        return {r["product"]: r["native_id"] for r in rows}

    def add_unresolved(self, agency: str, product: str, native_id: str, event_id: str,
                       event: CloudEvent | None = None) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO xw_unresolved(agency,product,native_id,event_id,payload) VALUES(?,?,?,?,?)",
            (agency, product, native_id, event_id, event.model_dump_json() if event else None))

    def unresolved(self, agency: str | None = None) -> list[dict]:
        q, p = "SELECT agency,product,native_id,event_id FROM xw_unresolved", ()
        if agency:
            q, p = q + " WHERE agency=?", (agency,)
        return [dict(r) for r in self.db.execute(q, p).fetchall()]

    def add_unresolved_in(self, conn: sqlite3.Connection, agency, product, native_id, event: CloudEvent) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO xw_unresolved(agency,product,native_id,event_id,payload) VALUES(?,?,?,?,?)",
            (agency, product, native_id, event.id, event.model_dump_json()))

    def resolve_in(self, conn: sqlite3.Connection, entry: CrosswalkEntry) -> list[CloudEvent]:
        """Add the mapping and pop parked events, inside the caller's transaction."""
        conn.execute(
            "INSERT INTO xw_entries VALUES(?,?,?,?,?) ON CONFLICT(agency,product,native_id) "
            "DO UPDATE SET entity=excluded.entity, canonical_id=excluded.canonical_id",
            (entry.agency, entry.product, entry.native_id, entry.entity, entry.canonical_id))
        rows = conn.execute(
            "SELECT event_id,payload FROM xw_unresolved WHERE agency=? AND product=? AND native_id=? ORDER BY rowid",
            (entry.agency, entry.product, entry.native_id)).fetchall()
        conn.execute("DELETE FROM xw_unresolved WHERE agency=? AND product=? AND native_id=?",
                     (entry.agency, entry.product, entry.native_id))
        out: list[CloudEvent] = []
        for r in rows:
            if r["payload"]:
                out.append(CloudEvent.model_validate_json(r["payload"]))
            elif self.bus is not None and (ev := self.bus.find("events.raw", r["event_id"])):
                out.append(ev)
        return out

    def resolve(self, entry: CrosswalkEntry) -> list[CloudEvent]:
        with self.db.tx() as c:
            return self.resolve_in(c, entry)
