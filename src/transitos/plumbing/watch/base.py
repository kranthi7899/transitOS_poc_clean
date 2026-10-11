"""Shared adapter machinery. Every poll writes cursor + last-seen + outbox rows in ONE DB transaction."""
from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from typing import Any

import httpx

from transitos.contracts.envelope import CloudEvent

from .. import outbox
from ..db import Db

STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS adapter_state(
  name TEXT PRIMARY KEY, cursor TEXT, last_seen TEXT NOT NULL DEFAULT '{}');
"""

Emitted = tuple[CloudEvent, str]  # (event, partition key)


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
        db.script(STATE_SCHEMA)
        outbox.ensure_schema(db)

    def get(self, path: str, **params: Any) -> Any:
        try:
            r = self.client.get(path, params={k: v for k, v in params.items() if v is not None},
                                headers={"X-Agency": self.agency})
        except HTTP_ERRORS as e:
            raise UpstreamError(f"{path}: {e!r}") from e
        if r.status_code >= 400:
            raise UpstreamError(f"{path}: http {r.status_code}")
        return r.json()

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
            if self.before_commit:
                self.before_commit()
            save_state(c, self.name, new_cursor, new_seen)
        return added
