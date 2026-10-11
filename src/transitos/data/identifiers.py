"""Synthetic vehicle identifiers: 49 CFR 565.15 VINs, fictional-jurisdiction plates, and seeded dirt.

The letter values and position weights below equal the tables extracted from the fetched regulation text
(fixtures/ecfr_565_15_example.json, checked by tests/data/test_identifiers.py). VINs made here are valid but
fictitious: their WMI is one that NHTSA vPIC did not recognise (fixtures/synthetic_wmis.yaml, `synthetic: true`,
checked once by transitos.data.vpic_wmi). Never look a synthetic VIN up in any registration, recall or history
service. Real WMIs live only in the opt-in fixtures/realistic_wmis.yaml. All products are SIMULATED.
"""
from __future__ import annotations

import random
from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]

# 49 CFR 565.15(c)(1) Table III; I, O and Q have no value and are never valid in a VIN
LETTER_VALUES: dict[str, int] = {
    "A": 1, "B": 2, "C": 3, "D": 4, "E": 5, "F": 6, "G": 7, "H": 8, "J": 1, "K": 2, "L": 3, "M": 4, "N": 5,
    "P": 7, "R": 9, "S": 2, "T": 3, "U": 4, "V": 5, "W": 6, "X": 7, "Y": 8, "Z": 9,
}
# 565.15(c)(2) Table IV / Table VI weight row: position 9 (the check digit) carries weight 0
WEIGHTS: tuple[int, ...] = (8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2)
# 565.15(d)(1) Table VII, the years this generator needs (2010-2030)
MODEL_YEAR_CODES: dict[int, str] = {
    2010: "A", 2011: "B", 2012: "C", 2013: "D", 2014: "E", 2015: "F", 2016: "G", 2017: "H", 2018: "J",
    2019: "K", 2020: "L", 2021: "M", 2022: "N", 2023: "P", 2024: "R", 2025: "S", 2026: "T", 2027: "V",
    2028: "W", 2029: "X", 2030: "Y",
}
VIN_CHARS = frozenset("0123456789" + "".join(LETTER_VALUES))


def _value(ch: str) -> int:
    if ch.isdigit():
        return int(ch)
    if ch in LETTER_VALUES:
        return LETTER_VALUES[ch]
    raise ValueError(f"character {ch!r} is not allowed in a VIN")


def vin_check_digit(vin17: str) -> str:
    """The 565.15(c) check digit for a 17-character VIN (whatever stands in position 9 is ignored: weight 0).
    Sum of value x weight, remainder mod 11; remainder 10 is 'X'. Raises ValueError on a wrong length or a
    character the regulation does not allow (I, O, Q, lower case, punctuation)."""
    if len(vin17) != 17:
        raise ValueError(f"a VIN has 17 characters, got {len(vin17)}")
    total = sum(_value(ch) * w for i, (ch, w) in enumerate(zip(vin17, WEIGHTS)) if i != 8)
    r = total % 11
    return "X" if r == 10 else str(r)


def is_valid_vin(vin: str | None) -> bool:
    """17 allowed characters and position 9 equals the computed check digit."""
    if not vin or len(vin) != 17:
        return False
    try:
        return vin[8] == vin_check_digit(vin)
    except ValueError:
        return False


def make_vin(wmi: str, vds: str, model_year: int, plant: str, serial: int) -> str:
    """WMI (positions 1-3) + VDS (4-8) + check digit (9) + model-year code (10) + plant (11) + positions 12-17.
    Buses and incomplete vehicles: the last four characters are numeric (565.15(d)); positions 12-13 hold a
    fixed '00' here (A75)."""
    if len(wmi) != 3 or len(vds) != 5 or len(plant) != 1:
        raise ValueError("wmi has 3 characters, vds 5, plant 1")
    if model_year not in MODEL_YEAR_CODES:
        raise ValueError(f"model year {model_year} outside the supported 2010-2030 range")
    if not 0 <= serial <= 9999:
        raise ValueError("serial must fit the numeric last four positions")
    body = f"{wmi}{vds}0{MODEL_YEAR_CODES[model_year]}{plant}00{serial:04d}"
    bad = set(body) - VIN_CHARS
    if bad:
        raise ValueError(f"characters not allowed in a VIN: {sorted(bad)}")
    return body[:8] + vin_check_digit(body) + body[9:]


@lru_cache(maxsize=1)
def _synthetic() -> tuple[str, ...]:
    doc = yaml.safe_load((ROOT / "fixtures" / "synthetic_wmis.yaml").read_text())
    if doc.get("synthetic") is not True:
        raise ValueError("fixtures/synthetic_wmis.yaml must be flagged synthetic: true")
    return tuple(doc["wmis"])


def synthetic_wmis() -> list[str]:
    """WMIs vPIC did not recognise when checked (see the fixture's provenance)."""
    return list(_synthetic())


_PLATE_LETTERS = "ABCDEFGHJKLMNPRSTUVWXYZ"   # no I, O, Q on the fictional plates (A76)


def make_plate(rng: random.Random, jurisdiction: str = "ZZ") -> str:
    """'<jurisdiction> <digit><3 letters><3 digits>', e.g. 'ZZ 4KTR219'. ZZ is a fictional jurisdiction (D-F)."""
    serial = (str(rng.randint(1, 9)) + "".join(rng.choice(_PLATE_LETTERS) for _ in range(3))
              + f"{rng.randint(0, 999):03d}")
    return f"{jurisdiction} {serial}"


def dirty_fleet_number(clean: str, rng: random.Random) -> str:
    """One seeded messy rendering of a fleet number like 'BUS-44': case, separators, a leading zero, a letter O
    typed for a zero, a trailing space. The digits' values never change, so normalisation can recover it."""
    prefix = "".join(ch for ch in clean if ch.isalpha())
    digits = "".join(ch for ch in clean if ch.isdigit())
    style = rng.randrange(6)
    if style == 0:
        return f"{prefix.lower()} {digits}"
    if style == 1:
        return f"{prefix}-0{digits}"
    if style == 2:
        return f"{prefix}-{digits} "
    if style == 3:
        return f"{prefix}{'O' + digits if rng.random() < 0.5 else digits.replace('0', 'O') or digits}"
    if style == 4:
        return f"{prefix.capitalize()} {digits}"
    return f"{prefix}_{digits}"


def dirty_vin(clean: str, rng: random.Random) -> str:
    """Seeded hand-typed rendering of a VIN: lower case, groups split by spaces or hyphens, letter O for zero.
    The characters' values never change, so a valid VIN stays valid after normalisation."""
    style = rng.randrange(4)
    if style == 0:
        return clean.lower()
    if style == 1:
        return f"{clean[:3]} {clean[3:9]} {clean[9:]}"
    if style == 2:
        return clean.replace("0", "O")
    return f"{clean[:8]}-{clean[8:]}".lower()


def dirty_plate(clean: str, rng: random.Random) -> str:
    """Seeded messy rendering of a plate: spacing, hyphens, case, letter O typed for zero."""
    compact = clean.replace(" ", "")
    style = rng.randrange(4)
    if style == 0:
        return compact.lower()
    if style == 1:
        return compact[:2] + "-" + compact[2:5] + " " + compact[5:]
    if style == 2:
        return compact.replace("0", "O")
    return " ".join(compact[i:i + 3] for i in range(0, len(compact), 3))
