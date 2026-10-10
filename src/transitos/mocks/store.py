"""Tiny SQLite JSON document store (per mock, tenant-scoped)."""
from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any


class Store:
    def __init__(self, db_path: Any = None) -> None:
        self._db = sqlite3.connect(str(db_path or ":memory:"), check_same_thread=False)
        self._lock = threading.RLock()
        with self._lock:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS docs(agency TEXT, coll TEXT, id TEXT, body TEXT,"
                " PRIMARY KEY(agency, coll, id))"
            )
            self._db.commit()

    def put(self, agency: str, coll: str, id: str, doc: dict) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO docs VALUES(?,?,?,?) ON CONFLICT(agency,coll,id) DO UPDATE SET body=excluded.body",
                (agency, coll, id, json.dumps(doc)),
            )
            self._db.commit()

    def get(self, agency: str, coll: str, id: str) -> dict | None:
        with self._lock:
            r = self._db.execute(
                "SELECT body FROM docs WHERE agency=? AND coll=? AND id=?", (agency, coll, id)
            ).fetchone()
        return json.loads(r[0]) if r else None

    def all(self, agency: str, coll: str) -> list[dict]:
        """Documents in insertion order."""
        with self._lock:
            rows = self._db.execute(
                "SELECT body FROM docs WHERE agency=? AND coll=? ORDER BY rowid", (agency, coll)
            ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def delete(self, agency: str, coll: str, id: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM docs WHERE agency=? AND coll=? AND id=?", (agency, coll, id))
            self._db.commit()

    def next_seq(self, agency: str, name: str) -> int:
        with self._lock:
            d = self.get(agency, "_seq", name) or {"n": 0}
            d["n"] += 1
            self.put(agency, "_seq", name, d)
            return d["n"]

    def clear(self, agency: str | None = None) -> None:
        with self._lock:
            if agency:
                self._db.execute("DELETE FROM docs WHERE agency=?", (agency,))
            else:
                self._db.execute("DELETE FROM docs")
            self._db.commit()
