"""SQLite implementation of contracts.crosswalk.Crosswalk (v1.2, B-1). The SQL is portable: the Postgres profile
runs it unchanged through `pg.translate` (PgDb).

A mapping row is keyed by (agency, product, native_id, valid_from): a native id may map to different canonical ids
over time (a telematics unit moved to another bus). Validity is valid_from inclusive, valid_to exclusive; open
bounds are unbounded. Bounds are stored as UTC ISO-8601 text ('' = open start, so it can sit in the key); naive
datetimes are read as UTC (A81). Time filtering happens in Python, not in SQL, so both dialects agree.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from transitos.contracts.crosswalk import CrosswalkEntry, Resolution
from transitos.contracts.envelope import CloudEvent

from .db import Db
from .resolver import utc, valid_at

_ENTRIES = """(
  agency TEXT NOT NULL, product TEXT NOT NULL, native_id TEXT NOT NULL, vf TEXT NOT NULL DEFAULT '',
  entity TEXT NOT NULL, canonical_id TEXT NOT NULL, valid_to TEXT,
  vin TEXT, plate TEXT, display_name TEXT,
  PRIMARY KEY(agency, product, native_id, vf))"""
SCHEMA = f"""
CREATE TABLE IF NOT EXISTS xw_entries{_ENTRIES};
CREATE INDEX IF NOT EXISTS ix_xw_canon ON xw_entries(agency, canonical_id);
CREATE TABLE IF NOT EXISTS xw_unresolved(
  agency TEXT NOT NULL, product TEXT NOT NULL, native_id TEXT NOT NULL,
  event_id TEXT NOT NULL, payload TEXT, resolution TEXT,
  PRIMARY KEY(agency, product, native_id, event_id));
"""
# v1 store (before B-1): no validity, no evidence, key (agency, product, native_id). Migrated by copy-and-rename so
# the new primary key's index name never collides with the old one's (Postgres names them per schema).
MIGRATE_V1 = f"""
CREATE TABLE xw_entries_v2{_ENTRIES};
INSERT INTO xw_entries_v2(agency, product, native_id, vf, entity, canonical_id)
  SELECT agency, product, native_id, '', entity, canonical_id FROM xw_entries;
DROP TABLE xw_entries;
ALTER TABLE xw_entries_v2 RENAME TO xw_entries;
CREATE INDEX IF NOT EXISTS ix_xw_canon ON xw_entries(agency, canonical_id);
"""
COLS = "agency,product,native_id,vf,entity,canonical_id,valid_to,vin,plate,display_name"


def _ts(dt: datetime | None) -> str | None:
    return utc(dt).isoformat() if dt is not None else None


def _dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def _entry(r: Any) -> CrosswalkEntry:
    return CrosswalkEntry(agency=r["agency"], product=r["product"], native_id=r["native_id"], entity=r["entity"],
                          canonical_id=r["canonical_id"], valid_from=_dt(r["vf"]), valid_to=_dt(r["valid_to"]),
                          vin=r["vin"], plate=r["plate"], display_name=r["display_name"])


def _current_first(e: CrosswalkEntry) -> tuple:
    """Sort key: open-ended rows first, then the latest end, then the latest start, then the native id."""
    end = e.valid_to.timestamp() if e.valid_to else float("inf")
    start = e.valid_from.timestamp() if e.valid_from else float("-inf")
    return (-end, -start, e.native_id, e.canonical_id)


class SqliteCrosswalk:
    def __init__(self, db: Db | str, bus=None):
        """bus (optional, a SqliteBus) lets resolve() recover a parked event body from events.raw when
        add_unresolved() was called without it (the contract signature carries only the event id)."""
        self.db = Db(db) if isinstance(db, str) else db
        self.bus = bus
        self.db.script(SCHEMA)
        self._migrate()

    def _has(self, table: str, column: str) -> bool:
        try:
            self.db.execute(f"SELECT {column} FROM {table} WHERE 1=0").fetchall()
            return True
        except Exception:  # noqa: BLE001 - sqlite3.OperationalError / psycopg UndefinedColumn
            return False

    def _migrate(self) -> None:
        if not self._has("xw_entries", "vf"):
            self.db.script(MIGRATE_V1)
        if not self._has("xw_unresolved", "resolution"):
            self.db.execute("ALTER TABLE xw_unresolved ADD COLUMN resolution TEXT")

    # ------------------------------------------------------------------ reads
    def _rows(self, agency: str, product: str, native_id: str) -> list[CrosswalkEntry]:
        rows = self.db.execute(f"SELECT {COLS} FROM xw_entries WHERE agency=? AND product=? AND native_id=?",
                               (agency, product, native_id)).fetchall()
        return sorted((_entry(r) for r in rows), key=_current_first)

    def rows(self, agency: str) -> list[CrosswalkEntry]:
        """Every mapping row of the agency (the resolver's namespace), deterministic order."""
        rows = self.db.execute(f"SELECT {COLS} FROM xw_entries WHERE agency=?", (agency,)).fetchall()
        return sorted((_entry(r) for r in rows), key=lambda e: (e.product, e.native_id, _current_first(e)))

    def candidates(self, agency: str, product: str, native_id: str,
                   at: datetime | None = None) -> list[CrosswalkEntry]:
        """Rows for this exact native id valid at `at`; without `at`, the current (open-ended) rows."""
        rows = self._rows(agency, product, native_id)
        if at is None:
            return [r for r in rows if r.valid_to is None]
        return [r for r in rows if valid_at(r, at)]

    def lookup(self, agency: str, product: str, native_id: str, at: datetime | None = None) -> str | None:
        c = self.candidates(agency, product, native_id, at)
        return c[0].canonical_id if c else None

    def all_ids(self, agency: str, canonical_id: str) -> dict[str, str]:
        """product -> the canonical id's CURRENT native id there (open-ended row first, else the latest one)."""
        rows = self.db.execute(f"SELECT {COLS} FROM xw_entries WHERE agency=? AND canonical_id=?",
                               (agency, canonical_id)).fetchall()
        out: dict[str, str] = {}
        for en in sorted((_entry(r) for r in rows), key=_current_first):
            out.setdefault(en.product, en.native_id)
        return out

    # ------------------------------------------------------------------ parked events
    def add_unresolved(self, agency: str, product: str, native_id: str, event_id: str,
                       event: CloudEvent | None = None) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO xw_unresolved(agency,product,native_id,event_id,payload) VALUES(?,?,?,?,?)",
            (agency, product, native_id, event_id, event.model_dump_json() if event else None))

    def unresolved(self, agency: str | None = None) -> list[dict]:
        q, p = "SELECT agency,product,native_id,event_id,resolution FROM xw_unresolved", ()
        if agency:
            q, p = q + " WHERE agency=?", (agency,)
        out = []
        for r in self.db.execute(q + " ORDER BY rowid", p).fetchall():
            d = dict(r)
            d["resolution"] = json.loads(d["resolution"]) if d.get("resolution") else None
            out.append(d)
        return out

    def add_unresolved_in(self, conn: sqlite3.Connection, agency, product, native_id, event: CloudEvent,
                          resolution: Resolution | None = None, notes: list[str] | None = None) -> None:
        res = None
        if resolution is not None or notes:
            res = json.dumps({**(resolution.model_dump(mode="json") if resolution else {}), "notes": list(notes or [])})
        conn.execute(
            "INSERT INTO xw_unresolved(agency,product,native_id,event_id,payload,resolution) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(agency,product,native_id,event_id) DO UPDATE SET resolution=excluded.resolution",
            (agency, product, native_id, event.id, event.model_dump_json(), res))

    # ------------------------------------------------------------------ writes
    def resolve_in(self, conn: sqlite3.Connection, entry: CrosswalkEntry) -> list[CloudEvent]:
        """Add the mapping (same native id and valid_from: overwrite) and pop parked events, inside the caller's
        transaction."""
        conn.execute(
            f"INSERT INTO xw_entries({COLS}) VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(agency,product,native_id,vf) DO UPDATE SET entity=excluded.entity, "
            "canonical_id=excluded.canonical_id, valid_to=excluded.valid_to, vin=excluded.vin, "
            "plate=excluded.plate, display_name=excluded.display_name",
            (entry.agency, entry.product, entry.native_id, _ts(entry.valid_from) or "", entry.entity,
             entry.canonical_id, _ts(entry.valid_to), entry.vin, entry.plate, entry.display_name))
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
