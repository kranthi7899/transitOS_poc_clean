"""Normalisation of native identity evidence (Phase 10 Step 2.3; fold rule amended in the fix round, F3, A141).

Every id: upper case, separators (space - _ . /) removed (`norm_serial`). Device serials and asset ids stop there:
SN-0012 and SN-12 are different devices. FLEET NUMBERS only (scheduling roster ids, display names, the 'bus 44'
family; `norm_fleet`): inside a numeric run (from its first digit on) a letter O reads as zero, and leading zeros of
a numeric run drop. A letter O before the first digit is part of the alphabetic prefix (TRO-101 is not TR-101) and
I is never read as 1. Missing evidence (None, blank, separators only) normalises to None: UNKNOWN, never a value.
"""
from __future__ import annotations

import re

_SEP = re.compile(r"[\s\-_./]+")
_NUMERIC_RUN = re.compile(r"[0-9][0-9O]*")


def _compact(s: str | None) -> str | None:
    if s is None:
        return None
    out = _SEP.sub("", str(s)).upper()
    return out or None


def norm_serial(s: str | None) -> str | None:
    """Device serial / asset id: case, whitespace and separators only (A141)."""
    return _compact(s)


def _fold_run(m: re.Match) -> str:
    return m.group(0).replace("O", "0").lstrip("0") or "0"


def norm_fleet(s: str | None) -> str | None:
    """Fleet number: 'bus 044', 'BUS-44 ', 'BUS-4O' (O inside the number) -> 'BUS44' / 'BUS40' (A78, A141)."""
    c = _compact(s)
    return _NUMERIC_RUN.sub(_fold_run, c) if c else None


def norm_vin(s: str | None) -> str | None:
    """VIN: upper case, separators removed; I, O and Q never stand in a VIN (49 CFR 565.15 Table III), so a typed
    O or Q is read as 0 and an I as 1. Validity (length, check digit) is judged by the caller."""
    c = _compact(s)
    if c is None:
        return None
    return c.replace("O", "0").replace("Q", "0").replace("I", "1")


def norm_plate(s: str | None) -> str | None:
    """Plate: upper case, separators removed, letter O read as zero (the simulated plates never use O; A76/A78;
    known gap for real plates)."""
    c = _compact(s)
    return c.replace("O", "0") if c else None
