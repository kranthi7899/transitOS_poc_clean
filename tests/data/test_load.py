from __future__ import annotations

import sqlite3

import pytest

from transitos.data.fetch import Manifest
from transitos.data.load import load_reference_db


def _setup(tmp_path):
    (tmp_path / "data/raw/t").mkdir(parents=True)
    (tmp_path / "data/raw/t/t.csv").write_text('a,"b c"\n1,x\n2,y\n')
    m = Manifest.model_validate({"version": 1, "user_agent": "u", "datasets": [
        {"id": "t", "kind": "api", "fetch": "auto", "url": "https://x/t", "format": "csv", "license": "PD",
         "target_path": "data/raw/t/t.csv"},
        {"id": "bad", "kind": "api", "fetch": "auto", "url": "https://x/b", "format": "csv",
         "target_path": "data/raw/bad/b.csv"},
    ]})
    lock = {"datasets": {
        "t": {"status": "fetched", "verified": True, "path": "data/raw/t/t.csv", "sha256": "abc", "bytes": 12,
              "fetched_at": "now", "http_status": 200, "url": "https://x/t"},
        "bad": {"status": "failed", "verified": False, "error": "boom"},
    }}
    return m, lock


def test_loads_verified_tables_and_provenance(tmp_path):
    m, lock = _setup(tmp_path)
    out = load_reference_db(m, lock, tmp_path)
    con = sqlite3.connect(tmp_path / "data/reference.db")
    assert con.execute("select count(*) from ref_t").fetchone()[0] == 2
    assert [r[1] for r in con.execute("pragma table_info(ref_t)")] == ["a", "b c"]
    assert con.execute("select dataset_id, sha256, rows from ref_provenance").fetchall() == [("t", "abc", 2)]
    assert out == {"t": 2}


def test_rerun_replaces_table(tmp_path):
    m, lock = _setup(tmp_path)
    load_reference_db(m, lock, tmp_path)
    load_reference_db(m, lock, tmp_path)
    con = sqlite3.connect(tmp_path / "data/reference.db")
    assert con.execute("select count(*) from ref_t").fetchone()[0] == 2
    assert con.execute("select count(*) from ref_provenance").fetchone()[0] == 1


def test_xlsx_loaded(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    (tmp_path / "data/raw/x").mkdir(parents=True)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Make", "Seats", "Year"])
    ws.append(["Gillig", 40, 2019])
    wb.save(tmp_path / "data/raw/x/x.xlsx")
    m = Manifest.model_validate({"version": 1, "user_agent": "u", "datasets": [
        {"id": "x", "kind": "file", "fetch": "auto", "url": "https://x/x", "format": "xlsx",
         "target_path": "data/raw/x/x.xlsx"}]})
    lock = {"datasets": {"x": {"status": "fetched", "verified": True, "path": "data/raw/x/x.xlsx"}}}
    assert load_reference_db(m, lock, tmp_path) == {"x": 1}
    con = sqlite3.connect(tmp_path / "data/reference.db")
    assert con.execute('select "Make","Seats" from ref_x').fetchall() == [("Gillig", 40)]
