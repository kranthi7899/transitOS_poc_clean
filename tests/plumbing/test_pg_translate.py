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
