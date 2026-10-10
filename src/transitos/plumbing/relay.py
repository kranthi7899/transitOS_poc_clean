"""Outbox relay: fetch unsent -> send -> flush -> mark sent. A crash between flush and mark republishes
(at-least-once); consumers dedupe by event id."""
from __future__ import annotations

from collections.abc import Callable

from transitos.contracts.bus import Producer
from transitos.contracts.envelope import CloudEvent

from . import outbox
from .db import Db


class Relay:
    def __init__(self, db: Db, producer: Producer, batch: int = 100):
        self.db, self.producer, self.batch = db, producer, batch
        outbox.ensure_schema(db)
        self.after_flush_hook: Callable[[], None] | None = None  # test seam: crash before marking sent

    def run_once(self) -> int:
        rows = self.db.execute(
            "SELECT seq,topic,key,payload FROM outbox WHERE sent_at IS NULL ORDER BY seq LIMIT ?", (self.batch,)
        ).fetchall()
        if not rows:
            return 0
        for r in rows:
            self.producer.send(r["topic"], r["key"], CloudEvent.model_validate_json(r["payload"]))
        self.producer.flush()
        if self.after_flush_hook:
            self.after_flush_hook()
        with self.db.tx() as c:
            c.executemany("UPDATE outbox SET sent_at=datetime('now') WHERE seq=?", [(r["seq"],) for r in rows])
        return len(rows)

    def drain(self) -> int:
        total = 0
        while n := self.run_once():
            total += n
        return total
