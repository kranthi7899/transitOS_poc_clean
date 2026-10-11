"""native_id normalisation, rows written from the policy text (Step 2.3): strip, case, separators, leading zeros,
O vs 0 in numeric positions. Missing evidence normalises to None (unknown), never to a value."""
from __future__ import annotations

import pytest

from transitos.plumbing.native_id import norm_fleet, norm_plate, norm_vin

FLEET = [
    ("BUS-44", "BUS44"), ("BUS-044", "BUS44"), ("bus 44", "BUS44"), ("BUS-44 ", "BUS44"), (" Bus 44", "BUS44"),
    ("BUSO44", "BUS44"), ("Bus O44", "BUS44"), ("BUS_44", "BUS44"), ("BUS.44", "BUS44"), ("bus/44", "BUS44"),
    ("BUS-4O", "BUS40"), ("BUS-1O6", "BUS106"), ("T-077", "T77"), ("t 77", "T77"), ("1106", "1106"),
    ("01106", "1106"), ("0", "0"), ("000", "0"), ("O1106", "1106"), ("A2000", "A2000"),
    ("B0B7", "B0B7"),   # an O/0 inside the alphabetic part is left alone: only the trailing numeric run is folded
]


@pytest.mark.parametrize("raw,want", FLEET)
def test_fleet_number_normalisation(raw, want):
    assert norm_fleet(raw) == want


@pytest.mark.parametrize("blank", [None, "", "   ", "-", " _ "])
def test_missing_evidence_is_unknown_not_a_value(blank):
    assert norm_fleet(blank) is None and norm_plate(blank) is None and norm_vin(blank) is None


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
