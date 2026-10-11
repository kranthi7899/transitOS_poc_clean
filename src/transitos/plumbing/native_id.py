"""Normalisation of native identity evidence (Phase 10 Step 2.3). Pure functions, no I/O.

Missing evidence (None, blank, separators only) normalises to None: it is UNKNOWN, never a value that could
equal or differ from anything (Step 1 review lesson).
"""
from __future__ import annotations

import re

_SEP = re.compile(r"[\s\-_./]+")
_TRAILING_NUMERIC = re.compile(r"[0-9O]+$")


def _compact(s: str | None) -> str | None:
    if s is None:
        return None
    out = _SEP.sub("", str(s)).upper()
    return out or None


def norm_fleet(s: str | None) -> str | None:
    """Fleet number / native id: upper case, separators removed; in the trailing numeric run (the run of digits and
    letter O at the end that contains at least one digit) a letter O is read as zero and leading zeros are dropped
    (A78). 'bus 044', 'BUS-44 ', 'Bus O44' -> 'BUS44'. Letters elsewhere are left alone."""
    c = _compact(s)
    if c is None:
        return None
    m = _TRAILING_NUMERIC.search(c)
    if not m or not any(ch.isdigit() for ch in m.group(0)):
        return c
    digits = m.group(0).replace("O", "0").lstrip("0") or "0"
    return c[:m.start()] + digits


def norm_vin(s: str | None) -> str | None:
    """VIN: upper case, separators removed; I, O and Q never stand in a VIN (49 CFR 565.15 Table III), so a typed
    O or Q is read as 0 and an I as 1. Validity (length, check digit) is judged by the caller."""
    c = _compact(s)
    if c is None:
        return None
    return c.replace("O", "0").replace("Q", "0").replace("I", "1")


def norm_plate(s: str | None) -> str | None:
    """Plate: upper case, separators removed, letter O read as zero (the simulated plates never use O; A76/A78)."""
    c = _compact(s)
    return c.replace("O", "0") if c else None
