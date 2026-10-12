"""SQLite -> PostgreSQL statement translation (pure; needs no server, runs in the default profile)."""
from __future__ import annotations

import pytest

from transitos.plumbing.pg import split_script, translate


def test_placeholders_and_literals_are_left_alone():
    assert translate("SELECT a FROM t WHERE x=? AND y='what?' AND z=?") == \
        "SELECT a FROM t WHERE x=%s AND y='what?' AND z=%s"
    assert translate("SELECT '100%' FROM t WHERE x=?") == "SELECT '100%%' FROM t WHERE x=%s"
    assert translate("SELECT '100%' FROM t", with_params=False) == "SELECT '100%' FROM t"


def test_insert_or_ignore_becomes_on_conflict_do_nothing():
    assert translate("INSERT OR IGNORE INTO outbox(event_id) VALUES(?)") == \
        "INSERT INTO outbox(event_id) VALUES(%s) ON CONFLICT DO NOTHING"


def test_insert_or_replace_uses_the_registered_key_and_unknown_tables_fail_loudly():
    assert translate("INSERT OR REPLACE INTO plans VALUES(?,?,?)") == \
        "INSERT INTO plans VALUES(%s,%s,%s) ON CONFLICT(run_id,plan_version) DO UPDATE SET plan=EXCLUDED.plan"
    with pytest.raises(NotImplementedError, match="unregistered"):
        translate("INSERT OR REPLACE INTO mystery VALUES(?)")


def test_sqlite_only_constructs_are_refused_not_guessed():
    for sql in ("PRAGMA journal_mode=WAL", "SELECT RAISE(ABORT,'x')"):
        with pytest.raises(NotImplementedError):
            translate(sql)


def test_reserved_word_autoincrement_rowid_time_and_two_arg_max():
    ddl = translate("CREATE TABLE IF NOT EXISTS m(offset INTEGER PRIMARY KEY AUTOINCREMENT, topic TEXT NOT NULL)")
    assert ddl == 'CREATE TABLE IF NOT EXISTS m("offset" BIGSERIAL PRIMARY KEY, topic TEXT NOT NULL)'
    t = translate("CREATE TABLE IF NOT EXISTS e(a TEXT PRIMARY KEY, b TEXT)")
    assert t.endswith(", rowid BIGSERIAL)")   # SQLite's implicit rowid, kept as the insertion-order column
    assert translate("UPDATE outbox SET sent_at=datetime('now') WHERE seq=?").startswith(
        "UPDATE outbox SET sent_at=to_char(now() AT TIME ZONE 'UTC'")
    assert translate("x=MAX(bus_offsets.committed,excluded.committed)") == \
        "x=GREATEST(bus_offsets.committed,excluded.committed)"
    assert translate("SELECT MAX(seq) FROM t") == "SELECT MAX(seq) FROM t"


def test_script_split_ignores_semicolons_in_literals_and_comments():
    parts = split_script("CREATE TABLE a(x TEXT DEFAULT ';'); -- c; d\nCREATE INDEX i ON a(x);")
    assert parts == ["CREATE TABLE a(x TEXT DEFAULT ';')", "CREATE INDEX i ON a(x)"]


def test_crosswalk_v12_statements_translate_without_sqlite_only_constructs():
    """Step 2 (B-1): every statement the crosswalk store runs is portable; the supervisor runs it on a server."""
    from transitos.plumbing import crosswalk as xw
    for stmt in split_script(xw.SCHEMA) + split_script(xw.MIGRATE_V1):
        t = translate(stmt, with_params=False)
        assert "?" not in t and "PRAGMA" not in t.upper()
    ddl = translate(split_script(xw.SCHEMA)[0], with_params=False)
    assert "vf TEXT NOT NULL DEFAULT ''" in ddl and ddl.endswith(", rowid BIGSERIAL)")
    assert translate(split_script(xw.MIGRATE_V1)[0], with_params=False).endswith(", rowid BIGSERIAL)")
    up = translate(f"INSERT INTO xw_entries({xw.COLS}) VALUES(?,?,?,?,?,?,?,?,?,?) "
                   "ON CONFLICT(agency,product,native_id,vf) DO UPDATE SET valid_to=excluded.valid_to")
    assert up.count("%s") == 10 and "ON CONFLICT(agency,product,native_id,vf)" in up
    assert translate("ALTER TABLE xw_unresolved ADD COLUMN resolution TEXT") == \
        "ALTER TABLE xw_unresolved ADD COLUMN resolution TEXT"


def test_crosswalk_fix_round_statements_translate():
    """F7 recovery statements (run inside one transaction) are portable; the merge keeps existing rows."""
    from transitos.plumbing import crosswalk as xw
    merge = translate(xw.RECOVER_V2["merge"][0], with_params=False)
    assert merge.startswith("INSERT INTO xw_entries(") and merge.endswith("FROM xw_entries_v2 ON CONFLICT DO NOTHING")
    assert translate(xw.RECOVER_V2["rename"][0], with_params=False) == "ALTER TABLE xw_entries_v2 RENAME TO xw_entries"
    for stmt in split_script(xw.MIGRATE_V1):
        assert "PRAGMA" not in translate(stmt, with_params=False).upper()
    # bug sweep A, G3: all_ids reads every row of the canonical id (validity and entity filtered in Python)
    assert translate(xw.ALL_IDS_SQL) == f"SELECT {xw.COLS} FROM xw_entries WHERE agency=%s AND canonical_id=%s"
    # G1 'device moved' closes the old row in place
    assert translate("UPDATE xw_entries SET valid_to=? WHERE agency=? AND product=? AND native_id=? AND vf=?") == \
        "UPDATE xw_entries SET valid_to=%s WHERE agency=%s AND product=%s AND native_id=%s AND vf=%s"


def test_poll_rejects_statements_translate():
    """Bug sweep B fix round (N2, A167): every poll_rejects statement the watch adapters run, translated exactly."""
    from transitos.plumbing.watch import base as wb
    (ddl,) = [translate(s, with_params=False) for s in split_script(wb.REJECT_SCHEMA)]
    assert ddl.startswith("CREATE TABLE IF NOT EXISTS poll_rejects(") and ddl.endswith(
        "PRIMARY KEY(poller, record_key), rowid BIGSERIAL)")
    for col in ("poller TEXT NOT NULL", "record_key TEXT NOT NULL", "native_id TEXT", "first_seen TEXT",
                "resolved INTEGER NOT NULL DEFAULT 0", "resolved_at TEXT"):
        assert col in ddl
    assert translate(wb.REJECT_UPSERT) == (
        "INSERT INTO poll_rejects(poller,record_key,agency,product,native_id,reason,digest,first_seen,last_seen,"
        "resolved,resolved_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,0,NULL) ON CONFLICT(poller,record_key) DO UPDATE "
        "SET native_id=excluded.native_id,reason=excluded.reason,digest=excluded.digest,last_seen=excluded.last_seen,"
        "resolved=0,resolved_at=NULL")
    assert translate(wb.REJECT_RESOLVE) == \
        "UPDATE poll_rejects SET resolved=1,resolved_at=%s WHERE poller=%s AND record_key=%s AND resolved=0"
    assert translate(wb.REJECT_OPEN_KEYS) == "SELECT record_key FROM poll_rejects WHERE poller=%s AND resolved=0"
    assert translate(wb.REJECT_LIST) == (
        "SELECT poller,record_key,agency,product,native_id,reason,digest,first_seen,last_seen,resolved,resolved_at "
        "FROM poll_rejects WHERE resolved<=%s ORDER BY poller,record_key")
