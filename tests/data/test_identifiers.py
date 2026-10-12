"""49 CFR 565.15 VIN check digit, synthetic VINs and plates (Phase 10 Step 2, task 2.1).

The worked example, letter values and weights come ONLY from the committed fixture, which was extracted from the
fetched eCFR XML by transitos.data.ecfr_vin (never from a transcription). The test re-implements nothing from the
module under test: it recomputes the expected digit from the fixture's own tables.
"""
from __future__ import annotations

import json
import random
from pathlib import Path

import pytest
import yaml

from transitos.data import identifiers as idf

ROOT = Path(__file__).resolve().parents[2]
FX = json.loads((ROOT / "fixtures" / "ecfr_565_15_example.json").read_text())
LOCK_SHA = "69e0a4224a2d13415f9d19cb39dad31c3e8da0bbaf5c9c1c4a0545bb26bfb744"   # MANIFEST.lock.json ecfr_565_15_api


def _independent_digit(vin: str) -> str:
    """From the fixture only: Table III values, Table VI weights, mod 11, remainder 10 -> X."""
    total = sum((int(c) if c.isdigit() else FX["letter_values"][c]) * w for c, w in zip(vin, FX["weights"]))
    r = total % 11
    return "X" if r == 10 else str(r)


def test_fixture_provenance_names_the_fetched_file():
    p = FX["_provenance"]
    assert p["sha256"] == LOCK_SHA and p["source_url"].startswith("https://www.ecfr.gov/")
    assert p["fetched_at"] and p["manifest_lock_entry"] == "ecfr_565_15_api"


def test_fixture_tables_are_self_consistent():
    # Table IV and Table VI agree on every weight; position 9 carries no weight
    assert FX["weights"][8] == 0 and FX["table_iv_weights"]["9"] == "(check digit)"
    assert all(int(FX["table_iv_weights"][str(i + 1)]) == w for i, w in enumerate(FX["weights"]) if i != 8)
    assert not {"I", "O", "Q"} & set(FX["letter_values"])
    ex = FX["example"]
    assert sum(ex["products"]) == ex["sum_of_products"]


def test_reproduces_the_regulations_worked_example():
    ex = FX["example"]
    vin = ex["vin_with_blank_position_9"]
    assert vin[8] == "_"
    for filler in "0X7":   # position 9 has weight 0: whatever stands there, the digit is the same
        assert idf.vin_check_digit(vin[:8] + filler + vin[9:]) == ex["check_digit"]


def test_module_tables_equal_the_fixture():
    assert idf.LETTER_VALUES == FX["letter_values"]
    assert list(idf.WEIGHTS) == FX["weights"]


@pytest.mark.parametrize("bad", ["I", "O", "Q"])
def test_i_o_q_are_never_valid(bad):
    vin = "1G4AH59H05G11834" + bad
    with pytest.raises(ValueError):
        idf.vin_check_digit(vin)
    assert idf.is_valid_vin("1G4AH59H45G11834" + bad) is False


def test_wrong_length_is_rejected():
    with pytest.raises(ValueError):
        idf.vin_check_digit("1G4AH59H45G1183")
    assert idf.is_valid_vin("") is False and idf.is_valid_vin(None) is False


def test_remainder_ten_is_x():
    base = "1G4AH59H05G1"
    for serial in range(100000):
        vin = f"{base}{serial:05d}"
        if _independent_digit(vin) == "X":
            assert idf.vin_check_digit(vin) == "X"
            assert idf.is_valid_vin(vin[:8] + "X" + vin[9:])
            return
    pytest.fail("no remainder-10 VIN found")


def test_check_digit_matches_independent_computation_on_random_vins():
    rng = random.Random(7)
    alphabet = "0123456789" + "".join(FX["letter_values"])
    for _ in range(300):
        vin = "".join(rng.choice(alphabet) for _ in range(17))
        assert idf.vin_check_digit(vin) == _independent_digit(vin)


def test_make_vin_structure():
    wmi = idf.synthetic_wmis()[0]
    vin = idf.make_vin(wmi, "B1C2D", 2024, "Z", 1234)
    assert len(vin) == 17 and vin[:3] == wmi
    assert vin[8] == _independent_digit(vin) and idf.is_valid_vin(vin)
    assert vin[9] == FX["model_year_codes"]["2024"] == "R"     # position 10: model year
    assert vin[10] == "Z"                                         # position 11: plant
    assert vin[13:].isdigit() and vin[13:] == "1234"              # buses: last four numeric
    assert idf.make_vin(wmi, "B1C2D", 2026, "Z", 1)[9] == FX["model_year_codes"]["2026"]


@pytest.mark.parametrize("args", [("1G", "B1C2D", 2024, "Z", 1), ("1GO", "B1C2D", 2024, "Z", 1),
                                  ("1G4", "B1C2", 2024, "Z", 1), ("1G4", "B1C2D", 2024, "I", 1),
                                  ("1G4", "B1C2D", 2004, "Z", 1), ("1G4", "B1C2D", 2024, "Z", 10000)])
def test_make_vin_rejects_bad_parts(args):
    with pytest.raises(ValueError):
        idf.make_vin(*args)


def test_synthetic_wmis_fixture_is_flagged_and_disjoint_from_real():
    doc = yaml.safe_load((ROOT / "fixtures" / "synthetic_wmis.yaml").read_text())
    assert doc["synthetic"] is True and doc["wmis"]
    real = yaml.safe_load((ROOT / "fixtures" / "realistic_wmis.yaml").read_text())
    assert real["opt_in"] is True
    assert not set(doc["wmis"]) & {r["wmi"] for r in real["wmis"]}
    assert idf.synthetic_wmis() == list(doc["wmis"])
    for w in doc["wmis"]:
        assert len(w) == 3 and not set(w) & set("IOQ")
        assert w.startswith("0")   # fix round F9: no WMI in a real region (ZZ7 was dropped: Z is Europe)


def test_make_plate_is_seeded_and_uses_the_fictional_jurisdiction():
    a = [idf.make_plate(random.Random(3)) for _ in range(3)]
    b = [idf.make_plate(random.Random(3)) for _ in range(3)]
    assert a == b
    p = idf.make_plate(random.Random(3))
    assert p.startswith("ZZ ") and len(p.split(" ")[1]) == 7
    assert idf.make_plate(random.Random(3), jurisdiction="XQ").startswith("XQ ")


def test_dirty_helpers_are_seeded_and_keep_the_identity_recoverable():
    rng1, rng2 = random.Random(5), random.Random(5)
    variants = [idf.dirty_fleet_number("BUS-44", rng1) for _ in range(20)]
    assert variants == [idf.dirty_fleet_number("BUS-44", rng2) for _ in range(20)]
    assert len(set(variants)) > 3
    for v in variants:   # dirt never changes the digits' values
        assert "".join(ch for ch in v.upper().replace("O", "0") if ch.isdigit()).lstrip("0") == "44"
    from transitos.plumbing.native_id import norm_vin
    vin = idf.make_vin(idf.synthetic_wmis()[0], "B1C2D", 2020, "Z", 1050)
    for seed in range(8):
        assert norm_vin(idf.dirty_vin(vin, random.Random(seed))) == vin
    pv = idf.dirty_plate("ZZ 4KT0219", random.Random(1))
    assert pv.replace(" ", "").replace("-", "").upper().replace("O", "0") == "ZZ4KT0219"
