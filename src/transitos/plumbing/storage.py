"""Selects the persistence backend: `TRANSITOS_DB=sqlite|postgres` (default sqlite, zero infra)."""
from __future__ import annotations

import os

DB_KINDS = ("sqlite", "postgres")


def db_kind(explicit: str | None = None) -> str:
    kind = (explicit or os.environ.get("TRANSITOS_DB") or "sqlite").strip().lower()
    if kind not in DB_KINDS:
        raise ValueError(f"TRANSITOS_DB must be one of {DB_KINDS}, got {kind!r}")
    return kind
