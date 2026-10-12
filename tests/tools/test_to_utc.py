"""Phase 10 Step 4.2: zone-less local time -> UTC (Review Focus 6).

Every expected row below was written FROM THE SPEC TEXT before the implementation existed:
  * fall-back (2026-11-01, America/New_York, 02:00 EDT -> 01:00 EST): 01:30 occurs twice. Documented rule: the
    FIRST occurrence (fold=0, EDT, UTC-4) unless the caller passes fold=1 or the product supplies a utc_offset.
  * spring-forward (2026-03-08, 02:00 EST -> 03:00 EDT): 02:00-02:59 does not exist. Rule: resolve FORWARD by the
    gap (02:30 -> 03:30 EDT = 07:30Z), whatever the fold.
  * a string that already carries a zone is a bug (mixed formats): ValueError.
  * a missing or unknown timezone is never guessed as UTC: ValueError (the adapter turns it into unavailable).
"""
from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from transitos.tools.common import UnknownTimezone, epoch_ms_to_utc, to_utc

NY = "America/New_York"


def z(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


# (naive local, kwargs, expected UTC) -- written from the rules above, not from the implementation
ROWS = [
    # ordinary days
    ("2026-10-10T02:00:00", {}, z("2026-10-10T06:00:00")),          # EDT, UTC-4
    ("2026-01-05T07:00:00", {}, z("2026-01-05T12:00:00")),          # EST, UTC-5
    # fall-back hour: 01:30 happens twice
    ("2026-11-01T00:59:00", {}, z("2026-11-01T04:59:00")),
    ("2026-11-01T01:30:00", {}, z("2026-11-01T05:30:00")),          # documented rule: first occurrence (EDT)
    ("2026-11-01T01:30:00", {"fold": 0}, z("2026-11-01T05:30:00")),
    ("2026-11-01T01:30:00", {"fold": 1}, z("2026-11-01T06:30:00")),  # second occurrence (EST)
    ("2026-11-01T01:00:00", {}, z("2026-11-01T05:00:00")),
    ("2026-11-01T01:00:00", {"fold": 1}, z("2026-11-01T06:00:00")),
    ("2026-11-01T01:59:59", {"fold": 1}, z("2026-11-01T06:59:59")),
    ("2026-11-01T02:00:00", {}, z("2026-11-01T07:00:00")),          # after the repeat: EST
    ("2026-11-01T01:30:00", {"utc_offset": "-04:00"}, z("2026-11-01T05:30:00")),   # the product disambiguates
    ("2026-11-01T01:30:00", {"utc_offset": "-05:00"}, z("2026-11-01T06:30:00")),
    ("2026-11-01T01:30:00", {"utc_offset": "-05:00", "fold": 0}, z("2026-11-01T06:30:00")),  # offset beats fold
    # spring-forward gap: 02:00-02:59 does not exist; resolve forward by the gap
    ("2026-03-08T01:59:00", {}, z("2026-03-08T06:59:00")),          # EST
    ("2026-03-08T02:00:00", {}, z("2026-03-08T07:00:00")),          # = 03:00 EDT
    ("2026-03-08T02:30:00", {}, z("2026-03-08T07:30:00")),          # = 03:30 EDT
    ("2026-03-08T02:30:00", {"fold": 1}, z("2026-03-08T07:30:00")),  # forward whatever the fold
    ("2026-03-08T02:59:59", {}, z("2026-03-08T07:59:59")),
    ("2026-03-08T03:00:00", {}, z("2026-03-08T07:00:00")),          # EDT
    ("2026-03-08T03:30:00", {}, z("2026-03-08T07:30:00")),
    # HH:MM on a given date
    ("01:30", {"on": date(2026, 11, 1)}, z("2026-11-01T05:30:00")),
    ("01:30", {"on": date(2026, 11, 1), "fold": 1}, z("2026-11-01T06:30:00")),
    ("02:30", {"on": date(2026, 3, 8)}, z("2026-03-08T07:30:00")),
    ("07:00", {"on": date(2026, 1, 5)}, z("2026-01-05T12:00:00")),
]


@pytest.mark.parametrize(("naive", "kw", "want"), ROWS, ids=[f"{r[0]}-{sorted(r[1].items())}" for r in ROWS])
def test_local_to_utc_golden_rows(naive, kw, want):
    got = to_utc(naive, NY, **kw)
    assert got == want
    assert got.tzinfo is not None and got.utcoffset().total_seconds() == 0


def test_fall_back_first_and_second_occurrence_are_an_hour_apart_and_ordered():
    first, second = to_utc("2026-11-01T01:30:00", NY), to_utc("2026-11-01T01:30:00", NY, fold=1)
    assert (second - first).total_seconds() == 3600


def test_spring_forward_gap_never_resolves_backward():
    # 01:59 EST is 06:59Z; a gap time must land AFTER it (forward), never before
    assert to_utc("2026-03-08T02:30:00", NY) > to_utc("2026-03-08T01:59:00", NY)
    assert to_utc("2026-03-08T02:30:00", NY, fold=1) > to_utc("2026-03-08T01:59:00", NY)


@pytest.mark.parametrize("zoned", ["2026-11-01T01:30:00Z", "2026-11-01T01:30:00+00:00", "2026-11-01T01:30:00-04:00",
                                   "2026-11-01T01:30:00.000-05:00"])
def test_a_string_that_already_carries_a_zone_raises(zoned):
    with pytest.raises(ValueError, match="zone"):
        to_utc(zoned, NY)


@pytest.mark.parametrize("bad", ["", "  ", "2026-11-01", "2026-11-01 01:30:00", "1:30", "01:30:00", "yesterday",
                                 "2026-11-01T25:00:00"])
def test_malformed_local_time_raises(bad):
    with pytest.raises(ValueError):
        to_utc(bad, NY, on=date(2026, 11, 1))


def test_hh_mm_needs_a_date():
    with pytest.raises(ValueError, match="date"):
        to_utc("01:30", NY)


@pytest.mark.parametrize("tz", [None, "", "Mars/Olympus_Mons", "not a zone"])
def test_missing_or_unknown_timezone_is_never_guessed_as_utc(tz):
    with pytest.raises(UnknownTimezone):
        to_utc("2026-10-10T02:00:00", tz)  # type: ignore[arg-type]
    assert issubclass(UnknownTimezone, ValueError)


@pytest.mark.parametrize("off", ["+00:00", "-06:00", "-04:00x", "EST"])
def test_a_utc_offset_that_contradicts_the_timezone_raises(off):
    # 01:30 on fall-back day is only ever -04:00 or -05:00 in New York; anything else means the product and the
    # agency config disagree about the zone: unknown, never a guess
    with pytest.raises(ValueError):
        to_utc("2026-11-01T01:30:00", NY, utc_offset=off)


def test_a_utc_offset_that_names_a_gap_wall_time_raises():
    # 02:30 never shows on a New York clock on 2026-03-08, so no offset can make it a real reading
    with pytest.raises(ValueError):
        to_utc("2026-03-08T02:30:00", NY, utc_offset="-05:00")


def test_fold_must_be_zero_or_one():
    with pytest.raises(ValueError):
        to_utc("2026-11-01T01:30:00", NY, fold=2)


def test_epoch_milliseconds():
    assert epoch_ms_to_utc(1_791_612_000_000) == z("2026-10-10T06:00:00")
    assert epoch_ms_to_utc(1_791_612_000_500) == datetime(2026, 10, 10, 6, 0, 0, 500_000, tzinfo=UTC)
    for bad in (True, "1791612000000", 1.5e12, None):
        with pytest.raises(ValueError):
            epoch_ms_to_utc(bad)  # type: ignore[arg-type]
