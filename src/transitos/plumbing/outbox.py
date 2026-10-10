"""Transactional outbox. Rows are inserted inside the caller's DB transaction; UNIQUE(event_id) drops repeats."""
from __future__ import annotations

import sqlite3

from transitos.contracts.envelope import CloudEvent

from .db import Db

SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT NOT NULL UNIQUE,
  topic TEXT NOT NULL, key TEXT NOT NULL, payload TEXT NOT NULL,
  sent_at TEXT);
"""


def ensure_schema(db: Db) -> None:
    db.script(SCHEMA)


def add(conn: sqlite3.Connection, event: CloudEvent, topic: str, key: str, dedupe_id: str | None = None) -> bool:
    """Insert into the outbox inside the caller's open transaction. Returns False if it was a repeat.
    dedupe_id defaults to the event id; use a prefixed id when one event goes to several topics."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO outbox(event_id,topic,key,payload) VALUES(?,?,?,?)",
        (dedupe_id or event.id, topic, key, event.model_dump_json()),
    )
    return cur.rowcount == 1
