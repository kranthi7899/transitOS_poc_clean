"""Helpers for the project-local Postgres (scripts/postgres.sh calls these).

    python -m transitos.plumbing.pg_local ensure-db   create the `transitos` database if missing
    python -m transitos.plumbing.pg_local purge       drop every test/scenario schema (keeps transitos_*)
"""
from __future__ import annotations

import sys

from .pg import _require, drop_schemas, dsn

DBNAME = "transitos"


def ensure_db() -> bool:
    """Create the database if it does not exist (connects to the maintenance DB). Returns True if created."""
    psycopg, _ = _require()
    base = dsn().rsplit("/", 1)[0] + "/postgres"
    with psycopg.connect(base, autocommit=True) as c:
        if c.execute("SELECT 1 FROM pg_database WHERE datname=%s", (DBNAME,)).fetchone():
            return False
        c.execute(f'CREATE DATABASE "{DBNAME}"')
        return True


def main(argv: list[str]) -> int:
    if argv == ["ensure-db"]:
        print("created database" if ensure_db() else "database exists", DBNAME)
        return 0
    if argv == ["purge"]:
        print(f"dropped {drop_schemas()} non-canonical schemas")
        return 0
    print("usage: python -m transitos.plumbing.pg_local ensure-db|purge", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
