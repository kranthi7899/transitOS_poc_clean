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
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from transitos.contracts.crosswalk import CrosswalkEntry, Resolution
from transitos.contracts.envelope import CloudEvent

from .db import Db
from .pg import split_script
from .resolver import VEHICLE_ENTITIES, utc, valid_at

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
# G3: every row of one canonical id; validity and entity are filtered in Python (both dialects agree)
ALL_IDS_SQL = f"SELECT {COLS} FROM xw_entries WHERE agency=? AND canonical_id=?"
RECOVER_V2 = {   # F7: what start-up does with an orphaned xw_entries_v2 (see SqliteCrosswalk._recover)
    "merge": (f"INSERT OR IGNORE INTO xw_entries({COLS}) SELECT {COLS} FROM xw_entries_v2",
              "DROP TABLE xw_entries_v2"),
    "redo": ("DROP TABLE xw_entries_v2",),
    "rename": ("ALTER TABLE xw_entries_v2 RENAME TO xw_entries",),
}


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
    def __init__(self, db: Db | str, bus=None, clock: Callable[[], datetime] | None = None):
        """bus (optional, a SqliteBus) lets resolve() recover a parked event body from events.raw when
        add_unresolved() was called without it (the contract signature carries only the event id). clock
        (optional; the System passes its simulated clock) is what 'now' means for all_ids without `at` (G3)."""
        self.db = Db(db) if isinstance(db, str) else db
        self.bus = bus
        self.clock = clock
        self._recover()
        self.db.script(SCHEMA)
        self._migrate()

    @staticmethod
    def _before_migration_statement(stmt: str) -> None:
        """Test seam (F7): called before each migration statement, inside the migration's transaction."""

    def _recover(self) -> None:
        """F7 (A141): an `xw_entries_v2` left behind by an interrupted migration (the pre-fix migration was not one
        transaction). Old table still there -> the half copy is discarded and the migration redone; no table ->
        the copy is the data, renamed into place; a new-schema table -> the copy's rows are merged back."""
        if not self._has("xw_entries_v2", "vf"):
            return
        # probe outside the transaction: on Postgres a failed probe would abort it
        new, old = self._has("xw_entries", "vf"), self._has("xw_entries", "agency")
        with self.db.tx() as c:
            for stmt in RECOVER_V2["merge" if new else "redo" if old else "rename"]:
                c.execute(stmt)

    def _has(self, table: str, column: str) -> bool:
        try:
            self.db.execute(f"SELECT {column} FROM {table} WHERE 1=0").fetchall()
            return True
        except Exception:  # noqa: BLE001 - sqlite3.OperationalError / psycopg UndefinedColumn
            return False

    def _migrate(self) -> None:
        if not self._has("xw_entries", "vf"):
            with self.db.tx() as c:   # F7: one transaction; a crash anywhere inside rolls the whole copy back
                for stmt in split_script(MIGRATE_V1):
                    self._before_migration_statement(stmt)
                    c.execute(stmt)
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

    def all_ids(self, agency: str, canonical_id: str, at: datetime | None = None,
                entities: tuple[str, ...] = VEHICLE_ENTITIES) -> dict[str, str]:
        """product -> the canonical id's native id there VALID at `at` (G3, A147-A148). Only rows whose entity is in
        `entities` count (default: a vehicle's vehicle/asset rows; operators pass ("operator",)). `at` None = now on
        this store's clock; a store built without a clock keeps the pre-G3 rule (open-ended row first, else the
        latest one: only stores no System owns, i.e. tests and tools, A147). A product with no valid row is left out
        (unknown), never filled from an ended or future-dated row. Several rows valid at once (a data conflict):
        the latest-starting one, deterministic."""
        rows = self.db.execute(ALL_IDS_SQL, (agency, canonical_id)).fetchall()
        ens = [en for en in (_entry(r) for r in rows) if en.entity in entities]
        if at is None and self.clock is not None:
            at = self.clock()
        if at is not None:
            ens = [en for en in ens if valid_at(en, at)]
        out: dict[str, str] = {}
        for en in sorted(ens, key=_current_first):
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

    # ------------------------------------------------------------------ human picks (F2, A141)
    def pick_in(self, conn: sqlite3.Connection, entry: CrosswalkEntry) -> list[CloudEvent]:
        """A human mapping never overwrites or shortens history. Without a valid_from it starts at the earliest
        parked event's own time for that native id. Refused (CrosswalkConflict, nothing written) when a row with
        the same key exists for another vehicle or with an end, or when the new window overlaps another vehicle's
        row for the same native id; an identical open row is left as it is."""
        key = (entry.agency, entry.product, entry.native_id)
        parked = [CloudEvent.model_validate_json(r["payload"]) for r in conn.execute(
            "SELECT payload FROM xw_unresolved WHERE agency=? AND product=? AND native_id=? ORDER BY rowid",
            key).fetchall() if r["payload"]]
        if entry.valid_from is None and parked:
            entry = entry.model_copy(update={"valid_from": min(utc(e.time) for e in parked)})
        vf = _ts(entry.valid_from) or ""
        rows = [_entry(r) for r in conn.execute(
            f"SELECT {COLS} FROM xw_entries WHERE agency=? AND product=? AND native_id=?", key).fetchall()]
        same = [r for r in rows if (_ts(r.valid_from) or "") == vf]
        if same and (same[0].canonical_id != entry.canonical_id or same[0].valid_to is not None):
            raise CrosswalkConflict(same[0])
        for r in rows:
            if r.canonical_id != entry.canonical_id and _overlaps(r, entry):
                raise CrosswalkConflict(r)
        covered = entry.valid_from is not None and any(
            r.canonical_id == entry.canonical_id and valid_at(r, entry.valid_from) for r in rows)
        if same or covered:   # the same mapping already holds at that time: release the parked events only
            return self._pop(conn, entry)
        return self.resolve_in(conn, entry)

    def pick(self, entry: CrosswalkEntry) -> list[CloudEvent]:
        with self.db.tx() as c:
            return self.pick_in(c, entry)

    # ------------------------------------------------------------------ device moved (G1, A144)
    def move_in(self, conn: sqlite3.Connection, entry: CrosswalkEntry) -> list[CloudEvent]:
        """'Device moved' from the panel: at time T (the entry's valid_from, else the earliest parked event's own
        time) the native id stops belonging to the vehicle it maps to and starts belonging to `entry.canonical_id`.
        The row valid at T is closed at T (history before T untouched) and a row for the new vehicle opens at T
        (ending where the closed row ended). A second identical move is a no-op; a move that would leave
        overlapping rows, or one with no row to close, is refused (CrosswalkConflict, nothing written)."""
        key = (entry.agency, entry.product, entry.native_id)
        parked = [CloudEvent.model_validate_json(r["payload"]) for r in conn.execute(
            "SELECT payload FROM xw_unresolved WHERE agency=? AND product=? AND native_id=? ORDER BY rowid",
            key).fetchall() if r["payload"]]
        at = entry.valid_from or (min(utc(e.time) for e in parked) if parked else None)
        rows = [_entry(r) for r in conn.execute(
            f"SELECT {COLS} FROM xw_entries WHERE agency=? AND product=? AND native_id=?", key).fetchall()]
        if at is None:   # nothing parked and no time given: only a repeat of a move already made is accepted
            if any(r.canonical_id == entry.canonical_id and r.valid_to is None for r in rows):
                return []
            raise CrosswalkConflict(entry, "a device move needs a time: no parked event and no valid_from")
        current = [r for r in rows if valid_at(r, at)]
        if current and all(r.canonical_id == entry.canonical_id for r in current):
            return self._pop(conn, entry)                     # already moved: no-op
        if len(current) != 1 or (current[0].valid_from is not None and utc(current[0].valid_from) >= utc(at)):
            raise CrosswalkConflict(current[0] if current else entry, "no single row to close at the move time")
        old = current[0]
        new = entry.model_copy(update={"valid_from": utc(at), "valid_to": old.valid_to, "vin": None, "plate": None,
                                       "display_name": None})
        for r in rows:
            if r is not old and _overlaps(r, new):
                raise CrosswalkConflict(r)
        conn.execute("UPDATE xw_entries SET valid_to=? WHERE agency=? AND product=? AND native_id=? AND vf=?",
                     (_ts(at), *key, _ts(old.valid_from) or ""))
        return self.resolve_in(conn, new)

    def move(self, entry: CrosswalkEntry) -> list[CloudEvent]:
        with self.db.tx() as c:
            return self.move_in(c, entry)

    def _pop(self, conn: sqlite3.Connection, entry: CrosswalkEntry) -> list[CloudEvent]:
        rows = conn.execute(
            "SELECT event_id,payload FROM xw_unresolved WHERE agency=? AND product=? AND native_id=? ORDER BY rowid",
            (entry.agency, entry.product, entry.native_id)).fetchall()
        conn.execute("DELETE FROM xw_unresolved WHERE agency=? AND product=? AND native_id=?",
                     (entry.agency, entry.product, entry.native_id))
        return [CloudEvent.model_validate_json(r["payload"]) for r in rows if r["payload"]]


class CrosswalkConflict(ValueError):
    """A human mapping that would overwrite or overlap existing history (F2). `conflict_row` is the row in the
    way (the panel answers 409 with it)."""

    def __init__(self, row: CrosswalkEntry, why: str = "conflicts with existing mapping"):
        self.conflict_row = row.model_dump(mode="json")
        super().__init__(f"{why}: {self.conflict_row}")


def _overlaps(a: CrosswalkEntry, b: CrosswalkEntry) -> bool:
    lo = max(utc(a.valid_from) if a.valid_from else datetime.min.replace(tzinfo=UTC),
             utc(b.valid_from) if b.valid_from else datetime.min.replace(tzinfo=UTC))
    hi = min(utc(a.valid_to) if a.valid_to else datetime.max.replace(tzinfo=UTC),
             utc(b.valid_to) if b.valid_to else datetime.max.replace(tzinfo=UTC))
    return lo < hi
