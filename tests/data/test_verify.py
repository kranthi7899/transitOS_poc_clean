"""Verify stage tests on tiny generated files (no network)."""
from __future__ import annotations

import json

import pytest

from transitos.data.verify import (
    flag_profile,
    inspect_csv,
    inspect_text,
    inspect_xlsx,
    verify_all,
)


def test_inspect_csv_header_rows_and_embedded_newline(tmp_path):
    p = tmp_path / "a.csv"
    p.write_text('"a","b"\n"x","multi\nline"\n"y","z"\n')
    r = inspect_csv(p)
    assert r["header"] == ["a", "b"] and r["rows"] == 2 and r["ragged_rows"] == 0


def test_flag_profile_counts_non_empty_and_distinct(tmp_path):
    p = tmp_path / "a.csv"
    p.write_text("v,v_1,w_questionable\n1,,\n2,Q,\n3,Q,x\n")
    prof = flag_profile(p)
    assert prof["v_1"] == {"non_empty": 2, "distinct": {"Q": 2}}
    assert prof["w_questionable"]["non_empty"] == 1
    assert "v" not in prof


def test_inspect_xlsx_sheets_and_real_headers(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "S1"
    ws.append(["A", "B", "C"])
    ws.append([1, 2, 3])
    ws2 = wb.create_sheet("S2")
    ws2.append(["Title only"])
    ws2.append(["X", "Y", "Z"])
    p = tmp_path / "a.xlsx"
    wb.save(p)
    r = inspect_xlsx(p)
    assert [s["name"] for s in r["sheets"]] == ["S1", "S2"]
    assert r["sheets"][0]["headers"] == ["A", "B", "C"] and r["sheets"][0]["header_row"] == 1
    assert r["sheets"][1]["headers"] == ["X", "Y", "Z"] and r["sheets"][1]["header_row"] == 2


def test_inspect_text_detects_bot_check_page(tmp_path):
    p = tmp_path / "x.html"
    p.write_text("<html><head><title>Federal Register :: Request Access</title></head></html>")
    r = inspect_text(p)
    assert r["bot_check_page"] is True and r["title"] == "Federal Register :: Request Access"
    q = tmp_path / "y.xml"
    q.write_text('<DIV8 N="565.15"><HEAD>x</HEAD><P>Table VI</P></DIV8>')
    assert inspect_text(q)["bot_check_page"] is False


def test_verify_all_writes_columns_json_and_report(tmp_path):
    (tmp_path / "data/raw/t").mkdir(parents=True)
    (tmp_path / "data/raw/t/t.csv").write_text("a,b\n1,2\n3,4\n")
    manifest = {"datasets": [
        {"id": "t", "kind": "api", "fetch": "auto", "url": "https://x/t.csv", "format": "csv", "expected_rows": 3,
         "target_path": "data/raw/t/t.csv"},
        {"id": "r", "kind": "repo", "fetch": "reference_only", "url": "https://x/r", "target_path": "data/raw/r/"},
        {"id": "f", "kind": "api", "fetch": "auto", "url": "https://x/f.csv", "format": "csv",
         "target_path": "data/raw/f/f.csv"},
    ]}
    lock = {"datasets": {
        "t": {"status": "fetched", "http_status": 200, "content_type": "text/csv", "path": "data/raw/t/t.csv"},
        "r": {"status": "reference_only"},
        "f": {"status": "failed", "error": "ConnectError: boom"},
    }}
    from transitos.data.fetch import Manifest
    m = Manifest.model_validate({"version": 1, "user_agent": "u", "datasets": manifest["datasets"]})
    res = verify_all(m, lock, tmp_path)
    cols = json.loads((tmp_path / "data/derived/t_columns.json").read_text())
    assert cols["columns"] == ["a", "b"] and cols["rows"] == 2
    assert res["t"]["verified"] is False  # expected 3 rows, observed 2: a difference, not a pass
    assert any("expected_rows" in d for d in res["t"]["differences"])
    assert res["r"]["verified"] is False and "reference_only" in res["r"]["note"]
    assert res["f"]["verified"] is False and "boom" in " ".join(res["f"]["differences"])
    report = (tmp_path / "data/FETCH_REPORT.md").read_text()
    assert "`t`" in report and "expected_rows" in report
    lock_out = json.loads((tmp_path / "data/MANIFEST.lock.json").read_text())
    assert lock_out["datasets"]["t"]["verified"] is False
