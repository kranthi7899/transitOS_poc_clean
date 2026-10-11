"""P2: verify what was actually downloaded; never trust the research digest.

Writes data/derived/<id>_columns.json (the REAL headers), data/FETCH_REPORT.md (every difference from what the
research said) and sets `verified` in data/MANIFEST.lock.json. Needs the `data` extra only for XLSX (openpyxl).
"""
from __future__ import annotations

import csv
import json
import re
import sys
from datetime import date
from pathlib import Path

from transitos.data.fetch import Manifest, load_manifest

csv.field_size_limit(10_000_000)

# What the research digest / manifest claimed (used only to print differences against observation).
RESEARCH_CLAIMS: dict[str, dict] = {
    "ntd_rvi_2024": {"size_bytes": 4_770_000, "note": "exact headers UNKNOWN; rows probably fleets, no VIN/plate"},
    "ntd_breakdowns_2022_2024": {
        "columns": ["agency", "city", "state", "ntd_id", "organization_type", "reporter_type", "report_year",
                    "uace_code", "uza_name", "primary_uza_population", "agency_voms", "mode", "mode_name",
                    "type_of_service", "mode_voms", "major_mechanical_failures", "other_mechanical_failures",
                    "total_mechanical_failures", "vehicle_passenger_car_revenue"],
    },
    "ntd_vehicle_type_count": {"n_columns": 97},
    "ntd_vehicle_maintenance_2024": {"size_bytes": 77_000},
}
SKIP_VALUE_COLUMNS = 12  # profile values for at most this many distinct values per flag column


def inspect_csv(path: Path) -> dict:
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader, [])
        rows = ragged = 0
        for row in reader:
            if not row:
                continue
            rows += 1
            if len(row) != len(header):
                ragged += 1
    return {"header": header, "rows": rows, "ragged_rows": ragged}


def flag_profile(path: Path) -> dict:
    """For columns that look like data-quality flags (`*_1`, `*_2`, `*questionable*`), count non-empty cells."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        cols = [c for c in (reader.fieldnames or []) if re.search(r"_[12]$|questionable", c)]
        prof = {c: {"non_empty": 0, "distinct": {}} for c in cols}
        numeric_only: dict[str, bool] = {}
        for row in reader:
            for c in cols:
                v = (row.get(c) or "").strip()
                if v:
                    numeric = numeric_only.setdefault(c, True) and bool(re.fullmatch(r"-?\d+(\.\d+)?", v))
                    numeric_only[c] = numeric
                    prof[c]["non_empty"] += 1
                    d = prof[c]["distinct"]
                    if v in d or len(d) < SKIP_VALUE_COLUMNS:
                        d[v] = d.get(v, 0) + 1
    # all-numeric columns are counts or buckets, not flags
    return {c: p for c, p in prof.items() if not numeric_only.get(c, False)}


def inspect_xlsx(path: Path) -> dict:
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    sheets = []
    for ws in wb.worksheets:
        header, header_row, n = [], None, 0
        for i, row in enumerate(ws.iter_rows(values_only=True), start=1):
            n += 1
            if header_row is None:
                cells = [c for c in row if c is not None and str(c).strip() != ""]
                if len(cells) >= 3:
                    header_row, header = i, [("" if c is None else str(c)) for c in row]
                    while header and header[-1] == "":
                        header.pop()
        sheets.append({"name": ws.title, "rows_including_header": n, "header_row": header_row, "headers": header})
    wb.close()
    return {"sheets": sheets}


def inspect_text(path: Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"<title>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
    title = m.group(1).strip() if m else None
    bot = bool(re.search(r"Request Access|captcha|are you a robot", title or text[:3000], re.IGNORECASE))
    return {"title": title, "bot_check_page": bot, "bytes": len(text.encode()), "has_table_vi": "Table VI" in text}


def _check(ok: bool | None) -> str:
    return "[x]" if ok else "[ ]"


def verify_dataset(ds, entry: dict, root: Path) -> dict:
    res: dict = {"id": ds.id, "fetch": ds.fetch, "differences": [], "notes": [], "checks": {}, "verified": False}
    claims = RESEARCH_CLAIMS.get(ds.id, {})
    if ds.fetch == "reference_only":
        res["note"] = "reference_only: never fetched, nothing to verify"
        return res
    if entry.get("status") == "failed" or entry.get("error"):
        res["differences"].append(f"fetch failed: {entry.get('error')}")
        return res
    res["http_status"] = entry.get("http_status")
    res["content_type"] = entry.get("content_type")
    res["checks"]["status_200"] = entry.get("http_status") == 200
    res["checks"]["content_type_seen"] = bool(entry.get("content_type"))
    files: list[Path] = []
    if entry.get("path"):
        files = [root / entry["path"]]
    for d in entry.get("downloaded", []):
        files.append(root / ds.target_path / d["file"])
    if not files:
        res["differences"].append("no file on disk to verify")
        return res
    ok_all = res["checks"]["status_200"]
    for f in files:
        if not f.exists():
            res["differences"].append(f"missing file {f.name}")
            ok_all = False
            continue
        suffix = f.suffix.lower()
        fres: dict = {"file": f.name, "bytes": f.stat().st_size}
        if suffix == ".csv":
            r = inspect_csv(f)
            fres.update(header=r["header"], rows=r["rows"], ragged_rows=r["ragged_rows"])
            res["checks"]["header_row"] = bool(r["header"])
            res["checks"]["row_count"] = r["rows"] > 0
            res["checks"]["parses"] = r["ragged_rows"] == 0
            if r["ragged_rows"]:
                res["differences"].append(f"{r['ragged_rows']} rows whose width differs from the header")
                ok_all = False
            if ds.expected_rows is not None and r["rows"] != ds.expected_rows:
                res["differences"].append(f"expected_rows {ds.expected_rows}, observed {r['rows']}")
                ok_all = False
            if ds.expected_rows is not None:
                res["checks"]["row_count_matches_expected"] = r["rows"] == ds.expected_rows
            if "n_columns" in claims and claims["n_columns"] != len(r["header"]):
                res["differences"].append(f"research said {claims['n_columns']} columns, observed {len(r['header'])}")
            if "columns" in claims:
                missing = [c for c in claims["columns"] if c not in r["header"]]
                extra = [c for c in r["header"] if c not in claims["columns"]]
                if missing:
                    res["differences"].append(f"claimed columns not present: {missing}")
                if extra:
                    res["notes"].append(f"columns present but not in the research list (informational): {extra}")
            fp = flag_profile(f)
            if fp:
                fres["flag_columns"] = fp
            _write_columns(root, ds.id if len(files) == 1 else f"{ds.id}__{f.stem}", f.name, r["header"], r["rows"])
        elif suffix in (".xlsx", ".xls"):
            r = inspect_xlsx(f)
            fres["sheets"] = r["sheets"]
            res["checks"]["sheets_listed"] = bool(r["sheets"])
            res["checks"]["parses"] = True
            res["checks"]["header_row"] = all(s["header_row"] for s in r["sheets"])
            res["checks"]["row_count"] = all(s["rows_including_header"] > 1 for s in r["sheets"])
            first = r["sheets"][0]
            _write_columns(root, ds.id if len(files) == 1 else f"{ds.id}__{f.stem}", f.name, first["headers"],
                           first["rows_including_header"] - first["header_row"], sheets=r["sheets"])
            if "size_bytes" in claims and abs(fres["bytes"] - claims["size_bytes"]) > 0.1 * claims["size_bytes"]:
                res["differences"].append(
                    f"research said about {claims['size_bytes']} bytes, observed {fres['bytes']}")
            if not r["sheets"] or not first["header_row"]:
                ok_all = False
        elif suffix in (".html", ".xml"):
            r = inspect_text(f)
            fres.update(r)
            res["checks"]["parses"] = not r["bot_check_page"]
            if r["bot_check_page"]:
                res["differences"].append(f"{f.name} is a bot-check page ('{r['title']}'), not the regulation text")
                ok_all = False
        elif suffix == ".json":
            try:
                doc = json.loads(f.read_text())
                fres["json_top_keys"] = sorted(doc)[:12] if isinstance(doc, dict) else "list"
                if isinstance(doc, dict) and doc.get("licenseId"):
                    fres["licenseId"] = doc["licenseId"]
                    res["notes"].append(f"Socrata licenseId observed: {doc['licenseId']}")
                res["checks"]["parses"] = True
            except ValueError as e:
                res["differences"].append(f"json does not parse: {e}")
                ok_all = False
        res.setdefault("files", []).append(fres)
    res["verified"] = ok_all and not res["differences"]
    return res


def _write_columns(root: Path, key: str, filename: str, columns: list[str], rows: int, sheets=None) -> None:
    out = root / "data" / "derived" / f"{key}_columns.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    doc = {"dataset": key, "file": filename, "columns": columns, "rows": rows}
    if sheets is not None:
        doc["sheets"] = [{"name": s["name"], "header_row": s["header_row"], "columns": s["headers"]} for s in sheets]
    out.write_text(json.dumps(doc, indent=1) + "\n")


def render_report(manifest: Manifest, results: dict, lock: dict) -> str:
    lines = [
        "# FETCH_REPORT",
        "",
        (f"Generated by `transitos.data.verify` on {date.today().isoformat()}. Every line is something the verifier "
         "observed in the downloaded file or the HTTP response; nothing is copied from the research digest. "
         "`verified` is true only when the file was fetched, parses, and has no difference from the manifest "
         "expectations. A difference does not always mean the data is unusable; read the line."),
        "",
    ]
    for ds in manifest.datasets:
        r = results.get(ds.id)
        if r is None:
            continue
        e = lock["datasets"].get(ds.id, {})
        lines += [f"## `{ds.id}` ({ds.fetch})", "", f"- URL: {ds.url}"]
        if e.get("sha256"):
            lines.append(f"- sha256 `{e['sha256'][:16]}...`, {e.get('bytes')} bytes, fetched {e.get('fetched_at')}")
        if r.get("note"):
            lines.append(f"- {r['note']}")
        c = r["checks"]
        if c:
            lines.append(
                f"- Checklist: {_check(c.get('status_200'))} status 200 ({r.get('http_status')}); "
                f"{_check(c.get('content_type_seen'))} content type (`{r.get('content_type')}`); "
                f"{_check(c.get('header_row'))} header row; {_check(c.get('row_count'))} row count; "
                f"{_check(c.get('sheets_listed'))} sheets; {_check(c.get('parses'))} parses")
        for fr in r.get("files", []):
            if "rows" in fr:
                lines.append(f"- `{fr['file']}`: {fr['rows']} data rows, {len(fr['header'])} columns "
                             f"(expected_rows in manifest: {ds.expected_rows})")
                for col, p in fr.get("flag_columns", {}).items():
                    lines.append(f"  - flag column `{col}`: {p['non_empty']} non-empty, values {p['distinct']}")
            if "sheets" in fr:
                for s in fr["sheets"]:
                    lines.append(f"- `{fr['file']}` sheet `{s['name']}`: header row {s['header_row']}, "
                                 f"{len(s['headers'])} columns, {s['rows_including_header']} rows incl. header")
            if fr.get("title"):
                lines.append(f"- `{fr['file']}`: title `{fr['title']}`, bot-check page: {fr['bot_check_page']}")
        if e.get("new_hosts_needing_approval"):
            lines.append(f"- NEW HOSTS NEEDING OWNER APPROVAL (not fetched): {e['new_hosts_needing_approval']}")
        if r["differences"]:
            lines.append("- Differences from the research / manifest:")
            lines += [f"  - {d}" for d in r["differences"]]
        else:
            lines.append("- Differences from the research / manifest: none observed")
        lines += [f"- Note: {n}" for n in r.get("notes", [])]
        lines.append(f"- verified: {r['verified']}")
        lines.append("")
    return "\n".join(lines)


def verify_all(manifest: Manifest, lock: dict, root: Path) -> dict:
    root = Path(root)
    results = {}
    for ds in manifest.datasets:
        entry = lock["datasets"].get(ds.id)
        if entry is None:
            continue
        results[ds.id] = verify_dataset(ds, entry, root)
        entry["verified"] = results[ds.id]["verified"]
    lock_path = root / "data" / "MANIFEST.lock.json"
    lock_path.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n")
    (root / "data").mkdir(exist_ok=True)
    (root / "data" / "FETCH_REPORT.md").write_text(render_report(manifest, results, lock))
    return results


def main(argv: list[str] | None = None) -> int:
    root = Path.cwd()
    manifest = load_manifest(root / "data" / "MANIFEST.yaml", extra=root / "data" / "EXTRA_SOURCES.yaml")
    lock = json.loads((root / "data" / "MANIFEST.lock.json").read_text())
    res = verify_all(manifest, lock, root)
    for k, v in res.items():
        print(f"{k}: verified={v['verified']} differences={len(v['differences'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
