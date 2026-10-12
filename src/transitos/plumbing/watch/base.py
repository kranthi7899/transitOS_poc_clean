"""Shared adapter machinery. Every poll writes cursor + last-seen + outbox rows + record rejects in ONE DB
transaction."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime
from typing import Any

import httpx

from transitos.contracts.envelope import CloudEvent
from transitos.tools.common import UnknownTimezone

from .. import outbox
from ..db import Db

STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS adapter_state(
  name TEXT PRIMARY KEY, cursor TEXT, last_seen TEXT NOT NULL DEFAULT '{}');
"""

Emitted = tuple[CloudEvent, str]  # (event, partition key)

# Bug sweep B (F7-R4, A155): per-record isolation. One product record whose time cannot be read is not emitted, not
# silently dropped and does not hold the other records: it is kept here, one row per (poller, record), until the
# product sends it again readable (then the row is marked resolved). Portable SQL (SQLite and Postgres, plumbing.pg).
# first_seen / last_seen / resolved_at are SIM time from the poller's clock; without a clock they stay NULL (unknown,
# never the wall clock).
REJECT_SCHEMA = """
CREATE TABLE IF NOT EXISTS poll_rejects(
  poller TEXT NOT NULL, record_key TEXT NOT NULL, agency TEXT NOT NULL, product TEXT NOT NULL, native_id TEXT,
  reason TEXT NOT NULL, digest TEXT NOT NULL, first_seen TEXT, last_seen TEXT,
  resolved INTEGER NOT NULL DEFAULT 0, resolved_at TEXT,
  PRIMARY KEY(poller, record_key));
"""
REJECT_COLS = ("poller", "record_key", "agency", "product", "native_id", "reason", "digest", "first_seen",
               "last_seen", "resolved", "resolved_at")
REJECT_UPSERT = (
    "INSERT INTO poll_rejects(poller,record_key,agency,product,native_id,reason,digest,first_seen,last_seen,"
    "resolved,resolved_at) VALUES(?,?,?,?,?,?,?,?,?,0,NULL) ON CONFLICT(poller,record_key) DO UPDATE SET "
    "native_id=excluded.native_id,reason=excluded.reason,digest=excluded.digest,last_seen=excluded.last_seen,"
    "resolved=0,resolved_at=NULL")
REJECT_RESOLVE = "UPDATE poll_rejects SET resolved=1,resolved_at=? WHERE poller=? AND record_key=? AND resolved=0"
REJECT_OPEN_KEYS = "SELECT record_key FROM poll_rejects WHERE poller=? AND resolved=0"
REJECT_LIST = f"SELECT {','.join(REJECT_COLS)} FROM poll_rejects WHERE resolved<=? ORDER BY poller,record_key"


class RecordUnreadable(Exception):
    """One product record whose time cannot be read (zone-less with the flag off, an offset that disagrees with the
    agency zone, an implausible epoch): rejected, never emitted with a guessed time (A155)."""


def digest(doc: Any) -> str:
    """A short digest of the record as the product sent it (tells a corrected record from the same bad one)."""
    return hashlib.sha1(json.dumps(doc, sort_keys=True, default=str).encode()).hexdigest()[:12]


def ensure_reject_schema(db: Any) -> None:
    db.script(REJECT_SCHEMA)


def list_rejects(conn_or_db: Any, poller: str | None = None, agency: str | None = None,
                 include_resolved: bool = False) -> list[dict]:
    """Reject rows (open ones only unless include_resolved), ordered by poller and record."""
    rows = conn_or_db.execute(REJECT_LIST, (1 if include_resolved else 0,)).fetchall()
    out = [{k: r[k] for k in REJECT_COLS} for r in rows]
    return [r for r in out if poller in (None, r["poller"]) and agency in (None, r["agency"])]


class UpstreamError(Exception):
    """Product unreachable / non-2xx. Independent of the HTTP client library (httpx.Client in production,
    Starlette's TestClient - built on httpx2, whose exception classes are not httpx's - in-process)."""


def _http_errors() -> tuple[type[BaseException], ...]:
    errs: tuple[type[BaseException], ...] = (httpx.HTTPError, OSError)
    try:
        import httpx2  # type: ignore[import-not-found]
        errs += (httpx2.HTTPError,)
    except ImportError:
        pass
    return errs


HTTP_ERRORS = _http_errors()


def make_event(agency: str, product: str, event_id: str, type_: str, time: str | datetime, data: dict[str, Any]) -> CloudEvent:
    return CloudEvent(
        id=event_id, source=f"adapter/{product}/{agency}", type=type_, time=time,  # type: ignore[arg-type]
        agencyid=agency, data=data,
    )


def zoned(value: Any) -> Any:
    """A product time that must carry its zone (no local-time setting for the agency): returned unchanged when it
    does; a zone-less value raises ValueError and is never read as UTC (A108, fix round F1)."""
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"zone-less timestamp {value!r} and the agency has no local_time setting: zone unknown (A108)")
    return value


def identity_evidence(vin: Any, plate: Any, name: Any) -> dict[str, str]:
    """Phase 10 Step 2 (B-1): the VIN, plate and name the product reported, as event data `native_vin`,
    `native_plate`, `native_name`. A blank or absent value is left out (unknown), never sent as ''."""
    out = {"native_vin": vin, "native_plate": plate, "native_name": name}
    return {k: str(v) for k, v in out.items() if v is not None and str(v).strip()}


def raw_key(product: str, native_id: str) -> str:
    return f"{product}:{native_id}"


def load_state(conn_or_db: Any, name: str) -> tuple[str | None, dict]:
    r = conn_or_db.execute("SELECT cursor,last_seen FROM adapter_state WHERE name=?", (name,)).fetchone()
    return (r["cursor"], json.loads(r["last_seen"])) if r else (None, {})


def save_state(conn: Any, name: str, cursor: str | None, last_seen: dict) -> None:
    conn.execute(
        "INSERT INTO adapter_state(name,cursor,last_seen) VALUES(?,?,?) "
        "ON CONFLICT(name) DO UPDATE SET cursor=excluded.cursor,last_seen=excluded.last_seen",
        (name, cursor, json.dumps(last_seen)),
    )


class Poller:
    """Subclasses implement build(cursor, last_seen) -> (events, new_cursor, new_last_seen) using self.client.
    Network I/O happens outside the transaction; the commit is atomic. Any failure before commit leaves the
    cursor untouched, so the next poll re-reads (outbox UNIQUE drops repeats)."""

    product = ""
    name = ""

    def __init__(self, db: Db, client: httpx.Client, agency: str):
        self.db, self.client, self.agency = db, client, agency
        self.name = self.name or f"{self.product}:{agency}"
        self.last_error: str | None = None
        self.before_commit: Callable[[], None] | None = None  # test seam: crash mid-poll
        # Phase 10 Step 4.2 (A108): how to read a product time `doc[key]` (zone-less local -> UTC). None = as sent.
        self.read_time: Callable[[dict, str], datetime] | None = None
        # bug sweep B (A155): the sim clock for the reject rows' seen times (System sets it); None = unknown
        self.clock: Callable[[], datetime] | None = None
        db.script(STATE_SCHEMA)
        ensure_reject_schema(db)
        outbox.ensure_schema(db)
        self._begin_records()

    def get(self, path: str, **params: Any) -> Any:
        try:
            r = self.client.get(path, params={k: v for k, v in params.items() if v is not None},
                                headers={"X-Agency": self.agency})
        except HTTP_ERRORS as e:
            raise UpstreamError(f"{path}: {e!r}") from e
        if r.status_code >= 400:
            raise UpstreamError(f"{path}: http {r.status_code}")
        return r.json()

    def ts(self, doc: dict, key: str) -> str | datetime:
        """The event time from a product record, never guessed. An unknown agency timezone is configuration, not a
        record: it holds the whole poll (UpstreamError: state kept, retried; A108, A157). Any other unreadable time
        rejects THIS record only (RecordUnreadable; A155)."""
        try:
            return zoned(doc[key]) if self.read_time is None else self.read_time(doc, key)
        except UnknownTimezone as e:
            raise UpstreamError(f"{self.product} timestamp {key}={doc.get(key)!r}: {e}") from e
        except ValueError as e:
            raise RecordUnreadable(f"{self.product} timestamp {key}={doc.get(key)!r}: {e}") from e

    # ---- bug sweep B (F7-R4, A155): per-record rejects, written in the poll's own transaction -----------------
    def _seen_at(self) -> str | None:
        return self.clock().isoformat() if self.clock is not None else None

    def _begin_records(self) -> None:
        self._rejects: list[tuple] = []
        self._resolved: list[str] = []
        self._open = {r["record_key"] for r in self.db.execute(REJECT_OPEN_KEYS, (self.name,)).fetchall()}

    def reject(self, record_key: str, native_id: Any, doc: Any, err: BaseException) -> None:
        """This record is not emitted; it is kept in poll_rejects and re-evaluated whenever it is seen again."""
        now = self._seen_at()
        self._rejects.append((self.name, record_key, self.agency, self.product,
                              None if native_id is None else str(native_id), str(err)[:300], digest(doc), now, now))

    def accept(self, record_key: str) -> None:
        """This record was read: a reject row it had is resolved."""
        if record_key in self._open:
            self._resolved.append(record_key)

    def _write_records(self, conn: Any) -> None:
        for row in self._rejects:
            conn.execute(REJECT_UPSERT, row)
        for key in self._resolved:
            conn.execute(REJECT_RESOLVE, (self._seen_at(), self.name, key))

    def status(self) -> dict:
        """The poller's health: last_error (the poll itself failed or is held) and its open record rejects."""
        rows = list_rejects(self.db, poller=self.name)
        return {"name": self.name, "last_error": self.last_error,
                "rejects": {"open": len(rows), "reasons": [r["reason"] for r in rows]}}

    def evidence(self, cache: dict, path: str, vin: str = "vin", plate: str = "plate", name: str = "name") -> dict:
        """Identity evidence for one record read from the product (cached for one poll). A failed read leaves the
        evidence unknown and never blocks the poll (A88): the crosswalk's exact path does not need it."""
        if path not in cache:
            try:
                d = self.get(path)
            except UpstreamError:
                d = None
            cache[path] = identity_evidence(d.get(vin), d.get(plate), d.get(name)) if isinstance(d, dict) else {}
        return cache[path]

    def build(self, cursor: str | None, last_seen: dict) -> tuple[list[Emitted], str | None, dict]:
        raise NotImplementedError

    def poll(self) -> int:
        """Returns number of NEW outbox rows written (0 on repeat or on upstream failure)."""
        cursor, last_seen = load_state(self.db, self.name)
        self._begin_records()
        try:
            events, new_cursor, new_seen = self.build(cursor, last_seen)
        except UpstreamError as e:  # timeout / 429 / 5xx: keep state, try again next tick
            self.last_error = repr(e)
            return 0
        self.last_error = None
        added = 0
        with self.db.tx() as c:
            for ev, key in events:
                added += outbox.add(c, ev, "events.raw", key)
            self._write_records(c)
            if self.before_commit:
                self.before_commit()
            save_state(c, self.name, new_cursor, new_seen)
        return added
