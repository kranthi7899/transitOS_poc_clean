"""Identity resolver, golden rows written from the policy text BEFORE the implementation (Step 2.3, D-E):

  order: normalise; exact VIN with check-digit validation (1.0; a bad check digit on a 17-character VIN is a
  dirty-VIN signal, not a match); exact normalised plate (0.95); rapidfuzz fleet number inside the agency
  namespace (0.6-0.9, 0.9 only for an exact normalised fleet number); keep only crosswalk rows valid at the event's
  own timestamp (valid_from inclusive, valid_to exclusive).
  MATCH if top >= 0.9 AND lead over the runner-up >= 0.1; AMBIGUOUS if two candidates within 0.1; a LONE candidate
  below 0.9 is AMBIGUOUS (a human decides); otherwise NO_MATCH. Missing evidence is unknown: never a match, never a
  mismatch. A native id the product used before but that has no row valid at the event time is never auto-matched.
Thresholds are ASSUMED (A79) and tested at their boundaries.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from transitos.contracts.crosswalk import Candidate, CrosswalkEntry
from transitos.data.identifiers import make_vin
from transitos.plumbing.resolver import Evidence, RulesResolver, StaticRows, decide

AG = "ag"
S = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)            # telematics unit swap on veh-44
T = datetime(2026, 10, 10, 6, 0, tzinfo=UTC)
VIN_A = make_vin("0ZB", "B1C2D", 2024, "Z", 44)
VIN_B = make_vin("0ZB", "B1C2D", 2024, "Z", 45)
BAD_VIN = VIN_A[:8] + ("1" if VIN_A[8] != "1" else "2") + VIN_A[9:]


def row(product, native, canon, entity="vehicle", agency=AG, **kw) -> CrosswalkEntry:
    return CrosswalkEntry(agency=agency, product=product, native_id=native, entity=entity, canonical_id=canon, **kw)


ROWS = [
    row("scheduling", "BUS-44", "veh-44", vin=VIN_A, plate="ZZ 4KTR219", display_name="Bus 44"),
    row("scheduling", "BUS-45", "veh-45", vin=VIN_B, plate="ZZ 5ABC103", display_name="Bus 45"),
    row("telematics", "T-77", "veh-44", valid_to=S),
    row("telematics", "T-90", "veh-44", valid_from=S),
    row("telematics", "T-78", "veh-45"),
    row("maintenance", "1106", "veh-44", entity="asset"),
    row("workforce", "OP-44", "op-1", entity="operator", display_name="Bus 44"),   # never a vehicle candidate
    row("scheduling", "BUS-44", "veh-x", agency="other", vin=VIN_B, display_name="Bus 44"),
]


def res(product="telematics", native="T-500", at=T, rows=ROWS, **ev):
    return RulesResolver(StaticRows(rows)).resolve(AG, product, Evidence(native_id=native, **ev), at)


def top(r):
    return (r.outcome, r.candidates[0].canonical_id if r.candidates else None, round(r.confidence, 4))


# ---- decide(): the outcome table ------------------------------------------------------------------------
def C(*scores):
    return [Candidate(canonical_id=f"v{i}", score=s, evidence=[]) for i, s in enumerate(scores)]


@pytest.mark.parametrize("scores,outcome", [
    ((), "NO_MATCH"),
    ((0.9,), "MATCH"),                      # lone, exactly at the threshold
    ((0.8999,), "AMBIGUOUS"),               # lone, just below: a human decides
    ((0.85,), "AMBIGUOUS"),                 # Review Focus 3: a lone 0.85 never auto-resolves
    ((0.6,), "AMBIGUOUS"),
    ((1.0, 0.9), "MATCH"),                  # lead exactly 0.1
    ((0.95, 0.85), "MATCH"),                # lead 0.1 in decimal, 0.0999... in floating point
    ((1.0, 0.95), "AMBIGUOUS"),             # two within 0.1
    ((0.95, 0.8501), "AMBIGUOUS"),
    ((0.9, 0.9), "AMBIGUOUS"),
    ((0.85, 0.8), "AMBIGUOUS"),             # two within 0.1, both below the threshold
    ((0.85, 0.7), "NO_MATCH"),              # not lone, below threshold, clear lead: "otherwise"
    ((0.95, 0.6, 0.6), "MATCH"),
])
def test_decide_table(scores, outcome):
    r = decide(C(*scores))
    assert r.outcome == outcome
    assert r.confidence == (max(scores) if scores else 0.0)


def test_decide_sorts_candidates_by_score_then_id():
    cands = [Candidate(canonical_id="b", score=0.7, evidence=[]), Candidate(canonical_id="a", score=0.7, evidence=[]),
             Candidate(canonical_id="c", score=0.9, evidence=[])]
    assert [c.canonical_id for c in decide(cands).candidates] == ["c", "a", "b"]


# ---- rule 1: VIN -------------------------------------------------------------------------------------------
def test_rule_vin_exact_with_dirty_formatting_is_1_0():
    dirty = " " + VIN_A.lower()[:8] + " " + VIN_A.lower()[8:].replace("0", "o") + " "
    assert top(res(vin=dirty)) == ("MATCH", "veh-44", 1.0)


def test_rule_vin_bad_check_digit_is_a_dirty_signal_not_a_match():
    r, notes = RulesResolver(StaticRows(ROWS)).resolve_detailed(AG, "telematics", Evidence("T-500", vin=BAD_VIN), T)
    assert r.outcome == "NO_MATCH" and r.candidates == []
    assert any("check digit" in n for n in notes)


def test_rule_vin_dirty_does_not_block_other_evidence():
    assert top(res(vin=BAD_VIN, name="bus 44")) == ("MATCH", "veh-44", 0.9)


def test_rule_vin_short_or_long_is_unknown():
    assert res(vin=VIN_A[:16]).outcome == "NO_MATCH" and res(vin=VIN_A + "1").outcome == "NO_MATCH"


# ---- rule 2: plate -----------------------------------------------------------------------------------------
@pytest.mark.parametrize("plate,want", [("zz-4ktr 219", "veh-44"), ("ZZ4KTR219", "veh-44"), ("zz 5abc1O3", "veh-45")])
def test_rule_plate_exact_normalised_is_0_95(plate, want):
    assert top(res(plate=plate)) == ("MATCH", want, 0.95)


def test_rule_plate_partial_is_not_a_plate_match():
    assert res(plate="4KTR219").outcome == "NO_MATCH"


# ---- rule 3: fleet number ---------------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["Bus 44", "BUS-044", "bus 44", "BUS-44 ", "Bus O44", "BUSO44", "bus_044"])
def test_rule_fleet_number_dirty_names_resolve_to_the_same_vehicle(name):
    """Review Focus 2: every dirty spelling resolves to veh-44, never a second vehicle."""
    r = res(name=name)
    assert top(r) == ("MATCH", "veh-44", 0.9)
    assert [c.canonical_id for c in r.candidates][:1] == ["veh-44"]


def test_rule_fleet_number_dirty_native_id_in_the_same_product():
    assert top(res(product="scheduling", native="bus-044")) == ("MATCH", "veh-44", 0.9)
    assert top(res(product="maintenance", native="01106")) == ("MATCH", "veh-44", 0.9)


def test_rule_fleet_number_other_products_native_ids_are_not_fleet_numbers():
    # telematics native ids are compared with telematics rows only; "1106" is a maintenance asset number
    assert res(product="telematics", native="1106").outcome == "NO_MATCH"


def test_rule_fleet_number_fuzzy_is_between_0_6_and_0_9_and_never_auto_matches():
    r = res(name="Bus 4")            # equally close to Bus 44 and Bus 45
    assert r.outcome == "AMBIGUOUS"
    assert [c.canonical_id for c in r.candidates] == ["veh-44", "veh-45"]
    assert all(0.6 <= c.score < 0.9 for c in r.candidates)
    assert r.candidates[0].score == r.candidates[1].score


def test_lone_fuzzy_candidate_below_threshold_goes_to_a_human():
    """Review Focus 3 through the real rules: a single fuzzy candidate below 0.9 is AMBIGUOUS, never MATCH."""
    r = res(name="Bus 4", rows=ROWS[:1])
    assert r.outcome == "AMBIGUOUS" and len(r.candidates) == 1 and r.candidates[0].score < 0.9


def test_rule_fleet_number_far_names_are_no_candidates():
    assert res(name="Yard Truck 3").outcome == "NO_MATCH"


# ---- combination ------------------------------------------------------------------------------------------
def test_conflicting_vin_and_plate_go_to_a_human():
    r = res(vin=VIN_A, plate="ZZ 5ABC103")
    assert r.outcome == "AMBIGUOUS" and [c.canonical_id for c in r.candidates] == ["veh-44", "veh-45"]


def test_vin_beats_an_exact_name_on_another_vehicle_by_exactly_0_1():
    assert top(res(vin=VIN_A, name="Bus 45")) == ("MATCH", "veh-44", 1.0)


def test_plate_and_exact_name_on_different_vehicles_are_ambiguous():
    assert res(plate="ZZ 4KTR219", name="Bus 45").outcome == "AMBIGUOUS"


def test_agreeing_evidence_lists_every_rule_that_fired():
    r = res(vin=VIN_A, plate="ZZ4KTR219", name="bus 44")
    assert top(r) == ("MATCH", "veh-44", 1.0)
    ev = " ".join(r.candidates[0].evidence)
    assert "vin" in ev and "plate" in ev and "fleet number" in ev


# ---- missing evidence ------------------------------------------------------------------------------------
@pytest.mark.parametrize("blank", [None, "", "   "])
def test_missing_evidence_is_never_a_match_and_never_a_mismatch(blank):
    assert res(vin=blank, plate=blank, name=blank).outcome == "NO_MATCH"
    # a blank plate next to a VIN is not "a different plate": the VIN still matches at 1.0
    assert top(res(vin=VIN_A, plate=blank, name=blank)) == ("MATCH", "veh-44", 1.0)


def test_a_row_with_blank_evidence_never_matches_blank_event_evidence():
    rows = [row("scheduling", "BUS-1", "veh-1", vin="", plate="", display_name=""),
            row("scheduling", "BUS-2", "veh-2")]
    assert res(rows=rows, vin="", plate="", name="").outcome == "NO_MATCH"


# ---- namespace and entity --------------------------------------------------------------------------------
def test_other_agencies_rows_are_never_candidates():
    assert top(res(vin=VIN_B)) == ("MATCH", "veh-45", 1.0)      # the other agency's VIN_B row maps to veh-x


def test_operator_rows_are_never_vehicle_candidates():
    rows = [r for r in ROWS if r.entity == "operator"]
    assert res(rows=rows, name="Bus 44").outcome == "NO_MATCH"


# ---- validity (Review Focus 4): valid_from inclusive, valid_to exclusive ---------------------------------
def test_dirty_old_unit_id_before_the_swap_resolves_to_the_old_vehicle():
    assert top(res(native="T-077", at=S - timedelta(seconds=1))) == ("MATCH", "veh-44", 0.9)


def test_old_unit_exactly_at_valid_to_is_not_the_old_vehicle():
    assert res(native="T-077", at=S).outcome == "NO_MATCH"


def test_new_unit_exactly_at_valid_from_is_the_new_vehicle():
    assert top(res(native="t 90", at=S)) == ("MATCH", "veh-44", 0.9)
    assert res(native="t 90", at=S - timedelta(microseconds=1)).outcome == "NO_MATCH"


def test_a_known_but_expired_native_id_is_never_auto_matched_by_other_evidence():
    r, notes = RulesResolver(StaticRows(ROWS)).resolve_detailed(
        AG, "telematics", Evidence("T-77", name="Bus 44", vin=VIN_A), S + timedelta(hours=1))
    assert r.outcome == "AMBIGUOUS" and r.candidates and r.candidates[0].canonical_id == "veh-44"
    assert any("no crosswalk row valid" in n for n in notes)


def test_rows_outside_their_validity_are_not_evidence():
    rows = [row("scheduling", "BUS-44", "veh-44", vin=VIN_A, valid_to=S),
            row("scheduling", "BUS-144", "veh-144", vin=VIN_A, valid_from=S)]   # VIN carried over to a renumber
    assert top(res(rows=rows, vin=VIN_A, at=S - timedelta(seconds=1))) == ("MATCH", "veh-44", 1.0)
    assert top(res(rows=rows, vin=VIN_A, at=S)) == ("MATCH", "veh-144", 1.0)


def test_naive_timestamps_are_read_as_utc():
    assert top(res(native="T-077", at=(S - timedelta(seconds=1)).replace(tzinfo=None))) == ("MATCH", "veh-44", 0.9)


# ---- determinism -----------------------------------------------------------------------------------------
def test_same_input_gives_the_same_resolution():
    a = res(name="Bus 4", rows=list(reversed(ROWS)))
    b = res(name="Bus 4")
    assert a == b
