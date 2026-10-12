"""native_id normalisation, rows written from the policy text (Step 2.3 as amended by the fix round, F3):
- every id: strip, case, separators (space - _ . /) removed;
- FLEET NUMBERS only (scheduling native ids, display names, the 'bus 44' family): letter O read as zero INSIDE a
  numeric run (after its first digit, never an alphabetic prefix) and leading zeros of a numeric run dropped;
- device serials and asset ids: case/whitespace/separators only (SN-0012 and SN-12 are different devices);
- never I -> 1 outside a VIN.
Missing evidence normalises to None (unknown), never to a value."""
from __future__ import annotations

import pytest

from transitos.plumbing.native_id import norm_fleet, norm_plate, norm_serial, norm_vin

FLEET = [
    ("BUS-44", "BUS44"), ("BUS-044", "BUS44"), ("bus 44", "BUS44"), ("BUS-44 ", "BUS44"), (" Bus 44", "BUS44"),
    ("BUS_44", "BUS44"), ("BUS.44", "BUS44"), ("bus/44", "BUS44"),
    ("BUS-4O", "BUS40"), ("BUS-1O6", "BUS106"), ("1106", "1106"), ("01106", "1106"), ("0", "0"), ("000", "0"),
    ("A2000", "A2000"), ("B0B7", "B0B7"),
    # F3: an O before the first digit is part of the alphabetic prefix, never a zero
    ("BUSO44", "BUSO44"), ("Bus O44", "BUSO44"), ("O1106", "O1106"),
    ("TRO-101", "TRO101"), ("TR-101", "TR101"), ("BO-12", "BO12"), ("B-12", "B12"),
    # F3 (m3): I is never folded to 1 in a fleet number
    ("BUS-I2", "BUSI2"), ("BUS-1I", "BUS1I"),
]


@pytest.mark.parametrize("raw,want", FLEET)
def test_fleet_number_normalisation(raw, want):
    assert norm_fleet(raw) == want


@pytest.mark.parametrize("a,b", [("TRO-101", "TR-101"), ("BO-12", "B-12"), ("BUS-I2", "BUS-12")])
def test_prefix_letters_never_fold_into_the_number(a, b):
    assert norm_fleet(a) != norm_fleet(b)


@pytest.mark.parametrize("raw,want", [("SN-0012", "SN0012"), ("sn 12", "SN12"), ("T-077", "T077"),
                                      ("t 77", "T77"), ("GW-O1", "GWO1"), ("01106", "01106"), ("0041", "0041")])
def test_serials_and_asset_ids_keep_zeros_and_letters(raw, want):
    assert norm_serial(raw) == want


def test_sn_0012_is_not_sn_12():
    assert norm_serial("SN-0012") != norm_serial("SN-12")


@pytest.mark.parametrize("blank", [None, "", "   ", "-", " _ "])
def test_missing_evidence_is_unknown_not_a_value(blank):
    assert norm_fleet(blank) is None and norm_plate(blank) is None and norm_vin(blank) is None
    assert norm_serial(blank) is None


@pytest.mark.parametrize("raw,want", [
    (" 1g4ah59h45g118341 ", "1G4AH59H45G118341"), ("1G4-AH59H 45G118341", "1G4AH59H45G118341"),
    ("1G4AH59H45G118341", "1G4AH59H45G118341"),
    ("1G4AH59H45G1183O1", "1G4AH59H45G118301"),   # I, O, Q never stand in a VIN: O->0, I->1, Q->0
    ("1G4AH59H45G11834I", "1G4AH59H45G118341"), ("1G4AH59H45G11834Q", "1G4AH59H45G118340"),
])
def test_vin_normalisation(raw, want):
    assert norm_vin(raw) == want


@pytest.mark.parametrize("raw,want", [("ZZ 4KTR219", "ZZ4KTR219"), ("zz-4ktr 219", "ZZ4KTR219"),
                                      ("ZZ 4KTR2O9", "ZZ4KTR209"), ("ZZ.4KTR.209", "ZZ4KTR209")])
def test_plate_normalisation(raw, want):
    assert norm_plate(raw) == want
