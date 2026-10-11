"""P3: load verified tables into SQLite data/reference.db (exploration and derive queries only; nothing at runtime
reads it). One table per dataset file: ref_<dataset id>, plus ref_provenance (one row per file).

Cell values are stored as TEXT for CSV (as delivered) and as the native Python value for XLSX; derive casts in SQL.
"""
from __future__ import annotations

import csv
import json
import re
import sqlite3
import sys
from pathlib import Path

from transitos.data.fetch import Manifest, load_manifest

csv.field_size_limit(10_000_000)


def _ident(s: str) -> str:
    return '"' + s.replace('"', '""') + '"'


def _table_name(ds_id: str, suffix: str | None = None) -> str:
    base = f"ref_{ds_id}" + (f"_{suffix}" if suffix else "")
    return re.sub(r"[^A-Za-z0-9_]", "_", base)


def _rows_csv(path: Path):
    f = open(path, newline="", encoding="utf-8-sig")  # noqa: SIM115  closed by the generator below
    reader = csv.reader(f)
    header = next(reader)

    def gen():
        try:
            for row in reader:
                if row:
                    yield row + [""] * (len(header) - len(row)) if len(row) < len(header) else row[: len(header)]
        finally:
            f.close()
    return header, gen()


def _rows_xlsx(path: Path):
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    it = ws.iter_rows(values_only=True)
    header = [str(c) if c is not None else f"col_{i}" for i, c in enumerate(next(it))]

    def gen():
        for row in it:
            if any(c is not None for c in row):
                yield list(row)[: len(header)]
        wb.close()
    return header, gen()


def load_reference_db(manifest: Manifest, lock: dict, root: Path) -> dict[str, int]:
    root = Path(root)
    db = root / "data" / "reference.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.execute("drop table if exists ref_provenance")
    con.execute("create table ref_provenance (dataset_id text, table_name text, file text, url text, sha256 text, "
                "bytes integer, fetched_at text, http_status integer, licence text, rows integer)")
    loaded: dict[str, int] = {}
    for ds in manifest.datasets:
        entry = lock["datasets"].get(ds.id, {})
        if not entry.get("verified"):
            continue
        files: list[Path] = []
        if entry.get("path"):
            files.append(root / entry["path"])
        files += [root / ds.target_path / d["file"] for d in entry.get("downloaded", [])]
        for f in files:
            if f.suffix.lower() == ".csv":
                header, rows = _rows_csv(f)
            elif f.suffix.lower() == ".xlsx":
                header, rows = _rows_xlsx(f)
            else:
                continue
            suffix = None if len(files) == 1 else f.stem
            table = _table_name(ds.id, suffix)
            con.execute(f"drop table if exists {_ident(table)}")
            con.execute(f"create table {_ident(table)} ({', '.join(_ident(h) for h in header)})")
            marks = ",".join("?" * len(header))
            con.executemany(f"insert into {_ident(table)} values ({marks})", rows)
            n = con.execute(f"select count(*) from {_ident(table)}").fetchone()[0]
            con.execute("insert into ref_provenance values (?,?,?,?,?,?,?,?,?,?)",
                        (ds.id, table, f.name, entry.get("url", ds.url), entry.get("sha256"), entry.get("bytes"),
                         entry.get("fetched_at"), entry.get("http_status"), ds.license, n))
            loaded[ds.id if suffix is None else f"{ds.id}/{f.stem}"] = n
    con.commit()
    con.close()
    return loaded


def main() -> int:
    root = Path.cwd()
    m = load_manifest(root / "data" / "MANIFEST.yaml", extra=root / "data" / "EXTRA_SOURCES.yaml")
    lock = json.loads((root / "data" / "MANIFEST.lock.json").read_text())
    for k, v in load_reference_db(m, lock, root).items():
        print(f"ref_{k}: {v} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
