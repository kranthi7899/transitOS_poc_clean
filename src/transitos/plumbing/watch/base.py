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


def make_event(agency: str, product: str, event_id: str, type_: str, time: str | datetime, data: dict[str, Any]) -> CloudEvent:
    return CloudEvent(
        id=event_id, source=f"adapter/{product}/{agency}", type=type_, time=time,  # type: ignore[arg-type]
        agencyid=agency, data=data,
    )


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
        r = self.client.get(path, params={k: v for k, v in params.items() if v is not None},
                            headers={"X-Agency": self.agency})
        r.raise_for_status()
        return r.json()

    def build(self, cursor: str | None, last_seen: dict) -> tuple[list[Emitted], str | None, dict]:
        raise NotImplementedError

    def poll(self) -> int:
        """Returns number of NEW outbox rows written (0 on repeat or on upstream failure)."""
        cursor, last_seen = load_state(self.db, self.name)
        try:
            events, new_cursor, new_seen = self.build(cursor, last_seen)
        except httpx.HTTPError as e:  # timeout / 429 / 5xx: keep state, try again next tick
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
