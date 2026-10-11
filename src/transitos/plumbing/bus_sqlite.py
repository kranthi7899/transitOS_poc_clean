"""SQLite bus (local profile): topics, ordering per key, per-group committed offsets, replay, at-least-once.

A single global autoincrement id is the offset, so order within a key (and a topic) is total.
poll() never advances the group; only commit() does (call it after side effects are durable).
"""
from __future__ import annotations

import zlib
from collections.abc import Iterator

from transitos.contracts.envelope import CloudEvent

from .db import Db

PARTITIONS = 4

SCHEMA = """
CREATE TABLE IF NOT EXISTS bus_messages(
  offset INTEGER PRIMARY KEY AUTOINCREMENT,
  topic TEXT NOT NULL, key TEXT NOT NULL, partition INTEGER NOT NULL,
  event_id TEXT NOT NULL, payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_bus_topic ON bus_messages(topic, offset);
CREATE INDEX IF NOT EXISTS ix_bus_event ON bus_messages(topic, event_id);
CREATE TABLE IF NOT EXISTS bus_offsets(
  topic TEXT NOT NULL, grp TEXT NOT NULL, committed INTEGER NOT NULL,
  PRIMARY KEY(topic, grp));
"""


class SqliteBus:
    """Implements contracts.bus.Producer and Consumer."""

    def __init__(self, db: Db | str):
        self.db = Db(db) if isinstance(db, str) else db
        self.db.script(SCHEMA)

    # Producer
    def send(self, topic: str, key: str, event: CloudEvent) -> None:
        part = zlib.crc32(key.encode()) % PARTITIONS
        self.db.execute(
            "INSERT INTO bus_messages(topic,key,partition,event_id,payload) VALUES(?,?,?,?,?)",
            (topic, key, part, event.id, event.model_dump_json()),
        )

    def flush(self) -> None:  # sends are synchronous and durable (autocommit)
        return None

    # Consumer
    def committed(self, topic: str, group: str) -> int:
        r = self.db.execute("SELECT committed FROM bus_offsets WHERE topic=? AND grp=?", (topic, group)).fetchone()
        return r["committed"] if r else 0

    def poll(self, topic: str, group: str, limit: int = 100) -> Iterator[tuple[int, CloudEvent]]:
        rows = self.db.execute(
            "SELECT offset,payload FROM bus_messages WHERE topic=? AND offset>? ORDER BY offset LIMIT ?",
            (topic, self.committed(topic, group), limit),
        ).fetchall()
        for r in rows:
            yield r["offset"], CloudEvent.model_validate_json(r["payload"])

    def commit(self, topic: str, group: str, offset: int) -> None:
        self.db.execute(
            "INSERT INTO bus_offsets(topic,grp,committed) VALUES(?,?,?) "
            "ON CONFLICT(topic,grp) DO UPDATE SET committed=MAX(bus_offsets.committed,excluded.committed)",
            (topic, group, offset),
        )

    # Admin / replay
    def reset_group(self, topic: str, group: str, offset: int = 0) -> None:
        """Replay: rewind a group (default to offset 0)."""
        self.db.execute(
            "INSERT INTO bus_offsets(topic,grp,committed) VALUES(?,?,?) "
            "ON CONFLICT(topic,grp) DO UPDATE SET committed=excluded.committed",
            (topic, group, offset),
        )

    def messages(self, topic: str) -> list[tuple[int, str, CloudEvent]]:
        rows = self.db.execute(
            "SELECT offset,key,payload FROM bus_messages WHERE topic=? ORDER BY offset", (topic,)
        ).fetchall()
        return [(r["offset"], r["key"], CloudEvent.model_validate_json(r["payload"])) for r in rows]

    def find(self, topic: str, event_id: str) -> CloudEvent | None:
        r = self.db.execute(
            "SELECT payload FROM bus_messages WHERE topic=? AND event_id=? ORDER BY offset LIMIT 1", (topic, event_id)
        ).fetchone()
        return CloudEvent.model_validate_json(r["payload"]) if r else None

