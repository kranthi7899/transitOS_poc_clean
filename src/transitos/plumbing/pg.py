"""PostgreSQL implementation of the persistence layer (profile `TRANSITOS_DB=postgres`).

`PgDb` has the same surface as `plumbing.db.Db` (`execute`, `script`, `tx`, `close`, `lock`) so the outbox, relay,
bus, crosswalk, watch adapters and audit run unchanged. Their SQL is written once, in the SQLite dialect the
default profile uses, and is translated here on the way to the server (see `translate`). The translation is
small and strict: any SQLite construct it does not know raises `NotImplementedError` instead of being guessed.

Isolation: one PostgreSQL *schema* per store (plumbing, runs, idem, ks, ckpt), named `<namespace>_<store>`,
playing the role of the separate SQLite files. `TRANSITOS_PG_NAMESPACE` picks the namespace (`none` = the
canonical `transitos`); by default every `System` gets its own, persisted so that `restart()` reconnects.

Not here (stays out of the contract): connection pooling, automatic reconnect after a server restart
(a restarted server needs `System.restart()`, which rebuilds every connection from the committed data).
"""
from __future__ import annotations

import os
import re
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager

DEFAULT_PORT = 54329
STORES = ("plumbing", "runs", "idem", "ks", "ckpt")
CANONICAL_NAMESPACE = "transitos"


class PgError(RuntimeError):
    """Postgres profile misconfigured or unreachable."""


def pg_available() -> bool:
    try:
        import psycopg  # noqa: F401
    except ImportError:
        return False
    return True


def _require():
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as e:  # pragma: no cover - exercised only without the extra
        raise ImportError("the Postgres profile needs psycopg: uv sync --extra postgres") from e
    return psycopg, dict_row


def pg_port() -> int:
    return int(os.environ.get("TRANSITOS_PG_PORT", DEFAULT_PORT))


def dsn() -> str:
    return os.environ.get("TRANSITOS_PG") or f"postgresql://transitos@127.0.0.1:{pg_port()}/transitos"


def server_reachable(timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", pg_port()), timeout=timeout):
            return True
    except OSError:
        return False


def safe_ident(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]{1,63}", name):
        raise ValueError(f"bad schema name {name!r}")
    return name


def schema_name(namespace: str, store: str) -> str:
    return safe_ident(f"{namespace}_{store}")


def connect(schema: str, *, autocommit: bool = True, connect_timeout: int = 10):
    """A connection whose search_path is `schema` (created if missing). Rows are dicts."""
    psycopg, dict_row = _require()
    schema = safe_ident(schema)
    try:
        conn = psycopg.connect(dsn(), autocommit=True, row_factory=dict_row, prepare_threshold=None,
                               connect_timeout=connect_timeout)
    except psycopg.OperationalError as e:
        raise PgError(f"cannot reach Postgres at {dsn()} (make postgres-up): {e}") from e
    conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
    conn.execute(f'SET search_path TO "{schema}"')
    conn.autocommit = autocommit
    return conn


# --------------------------------------------------------------------------- SQLite -> Postgres translation
_TS = "to_char(now() AT TIME ZONE 'UTC','YYYY-MM-DD HH24:MI:SS')"   # == SQLite's datetime('now')
# INSERT OR REPLACE needs the conflict target: table -> (columns in VALUES order, primary key)
_REPLACE: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "plans": (("run_id", "plan_version", "plan"), ("run_id", "plan_version")),
    "decisions": (("run_id", "plan_version", "record"), ("run_id", "plan_version")),
    "escalations": (("run_id", "detail"), ("run_id",)),
}
_LIT = re.compile(r"'(?:[^']|'')*'")
_MASK = re.compile(r"\x00(\d+)\x00")


def _close_paren(s: str, open_at: int) -> int:
    depth = 0
    for i in range(open_at, len(s)):
        depth += (s[i] == "(") - (s[i] == ")")
        if depth == 0:
            return i
    raise ValueError("unbalanced parentheses")


def translate(sql: str, with_params: bool = True) -> str:
    """One SQLite-dialect statement -> PostgreSQL."""
    sql = sql.replace("datetime('now')", _TS)
    lits: list[str] = []

    def mask(m: re.Match) -> str:
        lits.append(m.group(0))
        return f"\x00{len(lits) - 1}\x00"

    s = _LIT.sub(mask, sql)
    s = re.sub(r"--[^\n]*", "", s).strip().rstrip(";").strip()
    if not s:
        return ""
    if re.search(r"\b(PRAGMA|RAISE|WITHOUT\s+ROWID)\b", s, re.IGNORECASE):
        raise NotImplementedError(f"SQLite-only construct in: {sql!r}")
    s = s.replace("?", "%s")
    s = re.sub(r"\boffset\b", '"offset"', s, flags=re.IGNORECASE)
    s = re.sub(r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b", "BIGSERIAL PRIMARY KEY", s, flags=re.IGNORECASE)
    s = re.sub(r"\bCURRENT_TIMESTAMP\b", f"({_TS})", s, flags=re.IGNORECASE)
    s = re.sub(r"\bMAX\(([^(),]+),([^(),]+)\)", r"GREATEST(\1,\2)", s, flags=re.IGNORECASE)
    m = re.match(r"INSERT\s+OR\s+IGNORE\s+INTO\b", s, re.IGNORECASE)
    if m:
        s = "INSERT INTO" + s[m.end():] + " ON CONFLICT DO NOTHING"
    m = re.match(r"INSERT\s+OR\s+REPLACE\s+INTO\s+(\w+)\s+VALUES", s, re.IGNORECASE)
    if m:
        table = m.group(1).lower()
        if table not in _REPLACE:
            raise NotImplementedError(f"INSERT OR REPLACE into unregistered table {table!r} (add it to pg._REPLACE)")
        cols, pk = _REPLACE[table]
        sets = ",".join(f"{c}=EXCLUDED.{c}" for c in cols if c not in pk)
        s = f"INSERT INTO {m.group(1)} VALUES" + s[m.end():]
        s += f" ON CONFLICT({','.join(pk)}) DO UPDATE SET {sets}"
    m = re.match(r"CREATE\s+TABLE\b[^(]*\(", s, re.IGNORECASE)
    if m and "BIGSERIAL" not in s.upper():   # every table gets SQLite's implicit rowid (insertion order)
        end = _close_paren(s, m.end() - 1)
        s = s[:end] + ", rowid BIGSERIAL" + s[end:]

    def unmask(mm: re.Match) -> str:
        lit = lits[int(mm.group(1))]
        return lit.replace("%", "%%") if with_params else lit

    return _MASK.sub(unmask, s)


def split_script(script: str) -> list[str]:
    lits: list[str] = []

    def mask(m: re.Match) -> str:
        lits.append(m.group(0))
        return f"\x00{len(lits) - 1}\x00"

    body = re.sub(r"--[^\n]*", "", _LIT.sub(mask, script))
    parts = [p.strip() for p in body.split(";") if p.strip()]
    return [_MASK.sub(lambda mm: lits[int(mm.group(1))], p) for p in parts]


class PgCursor:
    """Cursor facade: rows are dicts; the emulated `rowid` column is hidden from `SELECT *` (as in SQLite)."""

    def __init__(self, cur, hide_rowid: bool):
        self._cur, self._hide = cur, hide_rowid

    def _row(self, r: dict | None) -> dict | None:
        if r is not None and self._hide:
            r.pop("rowid", None)
        return r

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount

    def fetchone(self):
        return self._row(self._cur.fetchone()) if self._cur.description else None

    def fetchall(self) -> list[dict]:
        return [self._row(r) for r in self._cur.fetchall()] if self._cur.description else []

    def __iter__(self) -> Iterator[dict]:
        return iter(self.fetchall())


def _run(conn, sql: str, params: tuple | None) -> PgCursor:
    p = tuple(params) if params else None
    t = translate(sql, with_params=p is not None)
    cur = conn.execute(t, p)
    return PgCursor(cur, hide_rowid=bool(re.match(r"\s*SELECT\s+\*", sql, re.IGNORECASE)))


class PgTx:
    """What `tx()` yields: the subset of sqlite3.Connection the plumbing code uses."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql: str, params: tuple = ()) -> PgCursor:
        return _run(self._conn, sql, params)

    def executemany(self, sql: str, seq) -> None:
        t = translate(sql, with_params=True)
        with self._conn.cursor() as cur:
            cur.executemany(t, [tuple(p) for p in seq])


class PgDb:
    """Drop-in for plumbing.db.Db on PostgreSQL: autocommit statements, explicit atomic `tx()`."""

    dialect = "postgres"

    def __init__(self, schema: str):
        self.path = schema
        self.schema = schema
        self.conn = connect(schema)
        self.lock = threading.RLock()

    @contextmanager
    def tx(self) -> Iterator[PgTx]:
        """One atomic transaction: commit on success, rollback on any exception."""
        with self.lock:
            self.conn.execute("BEGIN")
            try:
                yield PgTx(self.conn)
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    def execute(self, sql: str, params: tuple = ()) -> PgCursor:
        with self.lock:
            return _run(self.conn, sql, params)

    def script(self, sql: str) -> None:
        with self.lock:
            for stmt in split_script(sql):
                self.conn.execute(translate(stmt, with_params=False))

    executescript = script   # sqlite3-style name used by workflow.store.RunsIndex

    def close(self) -> None:
        self.conn.close()


def new_namespace() -> str:
    import uuid
    return f"t_{uuid.uuid4().hex[:12]}"


def namespace_from_env() -> str | None:
    """TRANSITOS_PG_NAMESPACE: unset -> None (caller picks); `none`/empty -> canonical."""
    env = os.environ.get("TRANSITOS_PG_NAMESPACE")
    if env is None:
        return None
    env = env.strip()
    return CANONICAL_NAMESPACE if env.lower() in ("", "none") else safe_ident(env)


def drop_schemas(prefix_not: str = CANONICAL_NAMESPACE + "_") -> int:
    """Drop every user schema except public and the canonical transitos_* ones. Returns how many."""
    psycopg, dict_row = _require()
    with psycopg.connect(dsn(), autocommit=True, row_factory=dict_row) as c:
        names = [r["schema_name"] for r in c.execute("SELECT schema_name FROM information_schema.schemata")]
        victims = [n for n in names if n not in ("public", "information_schema") and not n.startswith("pg_")
                   and not n.startswith(prefix_not)]
        for n in victims:
            c.execute(f'DROP SCHEMA "{n}" CASCADE')
    return len(victims)


def drop_namespace(namespace: str) -> None:
    """Drop every store schema of one namespace (test teardown)."""
    psycopg, _ = _require()
    with psycopg.connect(dsn(), autocommit=True) as c:
        for store in STORES:
            c.execute(f'DROP SCHEMA IF EXISTS "{schema_name(namespace, store)}" CASCADE')
