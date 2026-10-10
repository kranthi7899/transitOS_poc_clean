"""SQLite helpers shared by plumbing components (local profile, A10)."""
from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager


class Db:
    """Thin autocommit connection wrapper with an explicit transaction context."""

    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.lock = threading.RLock()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """One atomic transaction: commit on success, rollback on any exception."""
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, params)

    def script(self, sql: str) -> None:
        with self.lock:
            self.conn.executescript(sql)

    def close(self) -> None:
        self.conn.close()
