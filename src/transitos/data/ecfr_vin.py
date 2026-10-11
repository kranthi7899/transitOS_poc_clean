"""Extract the VIN check-digit rules and the worked example from the fetched eCFR text of 49 CFR 565.15.

    uv run python -m transitos.data.ecfr_vin <path to ecfr_565_15.xml> [--out fixtures/ecfr_565_15_example.json]

Reads the eCFR API XML (data/raw/regs/ecfr_565_15.xml, gitignored, fetched by `make data`) and writes a small
derived fixture: Table III (assigned letter values), the weight row of Table VI (Table IV prints "(check digit)" at
position 9; Table VI gives it weight 0), Table VII (model-year codes) and the Table VI sample VIN with its check
digit taken from the regulation's own sentence. Nothing here is typed in by hand: every value comes from the XML.
Tests read only the committed fixture (no network, no raw file).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

SOURCE_URL = "https://www.ecfr.gov/api/versioner/v1/full/2026-10-01/title-49.xml?part=565&section=565.15"


def _text(el: ET.Element) -> str:
    return " ".join("".join(el.itertext()).split())


def _table(root: ET.Element, title_prefix: str) -> ET.Element:
    for tbl in root.iter("TABLE"):
        cap = tbl.find("CAPTION")
        if cap is not None and _text(cap).startswith(title_prefix):
            return tbl
    raise ValueError(f"table {title_prefix!r} not found")


def _rows(tbl: ET.Element) -> list[list[str]]:
    body = tbl.find("TBODY")
    assert body is not None
    return [[_text(td) for td in tr.findall("TD")] for tr in body.findall("TR")]


def extract(xml_text: str) -> dict:
    root = ET.fromstring(xml_text.strip())
    letters: dict[str, int] = {}
    for (cell,) in _rows(_table(root, "Table III")):
        m = re.fullmatch(r"([A-Z]) = (\d+)", cell)
        if not m:
            raise ValueError(f"unexpected Table III cell {cell!r}")
        letters[m.group(1)] = int(m.group(2))
    sample = {r[0]: r[1:] for r in _rows(_table(root, "Table VI"))}
    vin_cells = sample["Sample VIN"]
    weights = [int(w) for w in sample["Weight Factor"]]
    products = [int(p) for p in sample["Multiply Assigned value times weight factor"]]
    assigned = sample["Assigned Value"]
    if len(vin_cells) != 17 or len(weights) != 17:
        raise ValueError("Table VI must have 17 positions")
    foot = _text(_table(root, "Table VI").find("TFOOT"))  # type: ignore[arg-type]
    m = re.search(r"indicates that “(\w)” is the check digit", foot)
    if not m:
        raise ValueError("worked-example check digit sentence not found")
    total = re.search(r"= (\d+)\.", foot)
    years = {r[0]: r[1] for r in _rows(_table(root, "Table VII"))}
    # Table IV (position -> weight) for cross-checking Table VI's weight row
    t4 = {i + 1: r[1] for i, r in enumerate(_rows(_table(root, "Table IV")))}
    return {
        "letter_values": letters,
        "weights": weights,
        "table_iv_weights": t4,
        "example": {
            "vin_with_blank_position_9": "".join(c if c else "_" for c in vin_cells),
            "assigned_values": [int(a) if a else None for a in assigned],
            "products": products,
            "sum_of_products": int(total.group(1)) if total else None,
            "check_digit": m.group(1),
        },
        "model_year_codes": years,
    }


def build_fixture(xml_path: Path, fetched_at: str, sha256_lock: str) -> dict:
    raw = xml_path.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    if actual != sha256_lock:
        raise ValueError(f"sha256 of {xml_path} is {actual}, MANIFEST.lock.json says {sha256_lock}")
    return {
        "_provenance": {
            "source_url": SOURCE_URL,
            "regulation": "49 CFR 565.15 (eCFR, point-in-time 2026-10-01)",
            "fetched_at": fetched_at,
            "sha256": sha256_lock,
            "manifest_lock_entry": "ecfr_565_15_api",
            "licence": "US Government work (public domain)",
            "derived_by": "transitos.data.ecfr_vin",
        },
        **extract(raw.decode("utf-8")),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("xml")
    ap.add_argument("--lock", required=True, help="path to data/MANIFEST.lock.json")
    ap.add_argument("--out", default="fixtures/ecfr_565_15_example.json")
    a = ap.parse_args(argv)
    lock = json.loads(Path(a.lock).read_text())["datasets"]["ecfr_565_15_api"]
    fx = build_fixture(Path(a.xml), lock["fetched_at"], lock["sha256"])
    Path(a.out).write_text(json.dumps(fx, indent=2, sort_keys=False) + "\n")
    print(f"wrote {a.out}: check digit {fx['example']['check_digit']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
