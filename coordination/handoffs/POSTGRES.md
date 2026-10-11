# Handoff: real Postgres segment

## Built
- `scripts/postgres.sh fetch|up|down|status|reset|purge` (+ `make postgres-up postgres-down postgres-status
  postgres-purge postgres-test verify-postgres`). Downloads the zonky.io "embedded-postgres-binaries" jar
  (upstream PostgreSQL 17.7, server only: postgres/initdb/pg_ctl, no psql) from Maven Central into
  `.local/postgres/17.7.0`, verifies its SHA-256 against the published `.sha256` before extracting, `initdb`s
  once into `.local/postgres-data` (trust auth, user `transitos`, listens on 127.0.0.1:54329 only, no unix
  socket), creates database `transitos` (`plumbing/pg_local.py ensure-db`). `purge` drops every schema except
  `public` and the canonical `transitos_*` ones. Nothing system-wide; Homebrew had no Postgres.
- `plumbing/pg.py`: `PgDb` (same surface as `plumbing.db.Db`: `execute`, `script`, `tx`, `close`, `lock`),
  a strict SQLite-to-PostgreSQL statement translator (`translate`, `split_script`), connection helper,
  namespace/schema helpers. All existing plumbing SQL is written once (SQLite dialect) and translated:
  `?`->`%s`, `INSERT OR IGNORE`->`ON CONFLICT DO NOTHING`, `INSERT OR REPLACE` (registered tables only),
  `AUTOINCREMENT`->`BIGSERIAL`, `offset` quoted, 2-arg `MAX`->`GREATEST`, `datetime('now')`/`CURRENT_TIMESTAMP`
  -> same `YYYY-MM-DD HH:MM:SS` text, and an emulated `rowid` column (insertion order; hidden from `SELECT *`).
  Unknown SQLite constructs (PRAGMA, RAISE, unregistered REPLACE) raise `NotImplementedError`, never guessed.
- `plumbing/pg_stores.py`: `PgIdemStore`, `PgKillSwitchStore`, `PgRunsIndex` (subclass of `workflow.store.RunsIndex`
  with only its connection swapped, so its SQL is not duplicated), `PgAuditedRunsIndex`, `make_pg_checkpointer`
  (langgraph-checkpoint-postgres `PostgresSaver`, so the checkpointer is Postgres too, cleanly).
- `plumbing/storage.py` (`db_kind`, `TRANSITOS_DB=sqlite|postgres`, default sqlite). `System(db=...)`; env
  `TRANSITOS_PG` (DSN, default `postgresql://transitos@127.0.0.1:54329/transitos`), `TRANSITOS_PG_PORT`,
  `TRANSITOS_PG_NAMESPACE` (default: one namespace per System persisted in `data_dir/pg-namespace` so
  `restart()` reconnects; `none` = canonical `transitos`). Each store is its own schema `<ns>_<store>`
  (plumbing, runs, idem, ks, ckpt), mirroring the separate SQLite files (the audit and run-index tables are both
  called `decisions`, so one shared schema would collide).
- `plumbing/audit.py`: the append-only guarantee on Postgres is a row-level trigger (`CREATE OR REPLACE TRIGGER`,
  message `<table> is append-only`, same text the tests match). `plumbing/bus_sqlite.py`: one SQL expression
  qualified (`MAX(bus_offsets.committed, excluded.committed)`) so it is valid on both; SQLite behaviour unchanged.
- `integration/system.py` (wiring only, same as the Kafka segment did): branches in `_build`, `pg_namespace()`,
  `close()`/`restart()` also close the Postgres idem/kill-switch connections. Nothing in contracts/, mocks/,
  tools/, workflow/ was touched.
- `psycopg[binary]` + `langgraph-checkpoint-postgres` are the optional extra `postgres`
  (`uv sync --extra postgres`); uv.lock gained only those packages (+ psycopg-pool, tzdata). Without the extra
  `transitos.plumbing` still imports; the Postgres profile raises ImportError naming the install command.
- `.gitignore`: added `verification-report-postgres.json`.

## Tests
- `tests/plumbing/test_storage_contract.py` (new, 12 tests x {sqlite, postgres}): Db tx/rollback and row shape,
  outbox, relay crash-before-mark, crosswalk, append-only audit, IdemStore, kill switch, RunsIndex (one active run
  per vehicle, dedupe, context, signals, plan/decision/escalation upserts), LangGraph checkpointer with an interrupt
  across a new connection. Same assertions on both backends.
- `tests/plumbing/test_pg_translate.py` (6, no server needed, runs in the default profile).
- The `db` fixture in `tests/plumbing/conftest.py` is parametrised: every existing plumbing/bus test now also runs on
  `PgDb` (Postgres params marked `postgres`, auto-skipped without a server/psycopg).
- `tests/postgres/test_postgres_profile.py` (9): whole system happy path with every store in its own schema and the
  checkpoint tables on the server; two Systems isolated; restart with a run awaiting approval; workflow consumer and
  crosswalk killed before commit; replay; unreachable server -> `PgError`; default is sqlite; and
  `postgres_admin` server restart mid-run (`TRANSITOS_PG_ADMIN=1`).
- Gating (`tests/conftest.py`): Postgres tests run when psycopg is installed and 127.0.0.1:54329 answers or
  `TRANSITOS_PG` is set (then an unreachable server fails, not skips); markers in pyproject.

## Results (run in this session)
- Postgres 17.7 (x86_64 build under Rosetta on this arm64 Mac); SHA-256 verified: yes (f3478bea...0cc1); start 0.2-0.3 s;
  idle RSS 34 MB.
- Default, server stopped: `make verify` -> `All checks passed!`, `338 passed, 83 skipped, 1 xfailed`, `23/23 scenarios
  passed` (was 320 passed, 24 skipped; +18 sqlite-side new tests; the extra skips are the Postgres/Kafka params).
- `make verify-postgres` (TRANSITOS_DB=postgres, TRANSITOS_PG_ADMIN=1: every System/World on Postgres; ruff + full
  pytest incl. the server restart test + Track F report): `All checks passed!`, `382 passed, 39 skipped, 1 xfailed in
  17.88s`, `23/23 scenarios passed`, purge dropped 815 schemas. All 39 skips are Kafka (extra not installed); the one
  xfail is the pre-existing strict xfail A48.
- `make postgres-test` (Postgres-marked only): `44 passed, 15 skipped` (the skips are the Kafka x Postgres combos).
- First attempt of everything passed; no bug surfaced beyond the translator design points listed under Built.
- Server stopped at the end (`scripts/postgres.sh down`).

## Differences found / decisions
1. A server restart kills open connections; there is no automatic reconnect. `System.restart()` (what the test does)
   rebuilds every connection from committed data. A long-lived deployment needs a pool/reconnect (not built).
2. Writers are not serialised by `BEGIN IMMEDIATE` as on SQLite; correctness rests on the unique constraints and
   `ON CONFLICT`, which every write already used. Only one connection per store per System, guarded by a lock.
3. `TRUNCATE` is not blocked by the append-only triggers (UPDATE/DELETE are). A real deployment should revoke it.
4. `INSERT OR REPLACE` is supported only for tables registered in `pg._REPLACE` (plans, decisions, escalations).
   If the workflow owner adds another one, the Postgres run index fails loudly until it is registered.

## Assumptions (numbered from A57; for the program lead to merge into docs/ASSUMPTIONS.md)
| A57 | The Postgres profile stores each SQLite file's content in its own schema `<namespace>_<store>`; per-System namespaces are persisted in the data dir, canonical names only with `TRANSITOS_PG_NAMESPACE=none`; test schemas are dropped by `scripts/postgres.sh purge` | integration/system.py, plumbing/pg.py | decision |
| A58 | SQL stays written once in the SQLite dialect and is translated for Postgres by `plumbing/pg.py`; the shared contract suite (`test_storage_contract.py`) is what keeps both backends identical. New SQL in plumbing/workflow/tools must stay inside the translator's vocabulary or extend it | plumbing/pg.py | decision |
| A59 | `PgRunsIndex` reuses `workflow.store.RunsIndex` and its private `_SCHEMA`, and `PgIdemStore` reuses `tools.idem_store._TYPES`; changes there are picked up automatically, but renaming those private names breaks the Postgres profile | plumbing/pg_stores.py | open |
| A60 | Local Postgres is the zonky.io build of upstream PostgreSQL 17.7 (x86_64 binary under Rosetta on Apple Silicon), trust auth on 127.0.0.1 only. A deployed database needs authentication, TLS, backups and a supported PostgreSQL build | scripts/postgres.sh | open |
| A61 | No connection pooling or reconnect: one connection per store per System; a server restart needs `System.restart()` | plumbing/pg.py | open |

## Decisions needed from the owner
1. Pooling/reconnect for a long-running process (A61) before any real deployment.
2. Whether `TRUNCATE`/role grants for the audit tables are part of the POC or deployment hardening (difference 3).
3. Whether a single schema with prefixed table names is preferred over one schema per store (A57); the latter keeps
   table names identical to the SQLite files.

## Not done / not verified
- Kafka together with Postgres (`TRANSITOS_BUS=kafka TRANSITOS_DB=postgres`): the combination is wired (the bus factory and
  the DB factory are independent) and the `bus x db` test matrix includes it, but no broker was running here, so it was
  not executed.
- Single-process only; no concurrent-writer stress test; no TLS/roles; Windows/Linux fetch targets in the script are
  untested (only darwin-arm64 ran). `make demo` has no Postgres-specific target (run it with `TRANSITOS_DB=postgres`).
- `.local/pgtry/` (a scratch extraction I made while exploring) is left behind; it is gitignored and safe to delete.

Note: the assumptions listed here as A57-A61 were renumbered A60-A64 in docs/ASSUMPTIONS.md (A57-A59 are the undo rule).
