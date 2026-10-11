"""Ranking and search (plan 1.4): hard rules decide the list, then tier, unverifiable, warnings, soft score."""
from __future__ import annotations

from test_clearance_rules import FAR, POL, T0, blk, facts, opr, veh

from transitos.clearance import Assign, Clearance, Option, rank_options
from transitos.clearance.rank import greedy_option, rank_plans, soft_score
from transitos.contracts.clearance import ClearanceItem


def clr(unv=0, warn=0, tier="exact", excluded=False) -> Clearance:
    return Clearance(items=[ClearanceItem(rule="r", status="pass", basis="native")], excluded=excluded,
                     warnings=warn, unverifiable=unv, tier=tier)


def o(tag: str) -> Option:
    return Option((Assign("b1", tag),))


def test_excluded_options_are_dropped_and_reason_says_when_none_left():
    r = rank_options([(o("a"), clr(excluded=True), 0.0)])
    assert r.options == [] and "no option" in r.reason_first_beat_second


def test_order_is_unverifiable_then_warnings_then_soft_score():
    r = rank_options([(o("a"), clr(unv=1), 9.0), (o("b"), clr(warn=2), 0.0), (o("c"), clr(warn=1), 0.0),
                      (o("d"), clr(warn=1), 1.0)])
    assert [x.option.assignments[0].spare_id for x in r.options] == ["d", "c", "b", "a"]
    assert [x.rank for x in r.options] == [1, 2, 3, 4]


def test_exact_tier_beats_larger_tier_even_when_larger_looks_cleaner_otherwise():
    r = rank_options([(o("larger"), clr(tier="larger", warn=1), 5.0), (o("exact"), clr(unv=3), 0.0)])
    assert r.options[0].option.assignments[0].spare_id == "exact"
    assert "exact vehicle type" in r.reason_first_beat_second


def test_top3_keeps_three_and_a_one_line_reason():
    r = rank_options([(o(x), clr(unv=i), 0.0) for i, x in enumerate("abcde")])
    assert len(r.top3) == 3 and len(r.options) == 5
    assert r.reason_first_beat_second == "plan 1 beat plan 2: 0 vs 1 unverifiable item(s)"
    assert "\n" not in r.reason_first_beat_second


def test_reason_for_each_deciding_term():
    assert "warning" in rank_options([(o("a"), clr(warn=0), 0), (o("b"), clr(warn=1), 0)]).reason_first_beat_second
    assert "soft score" in rank_options([(o("a"), clr(), 2.0), (o("b"), clr(), 1.0)]).reason_first_beat_second
    assert "equal" in rank_options([(o("a"), clr(), 1.0), (o("b"), clr(), 1.0)]).reason_first_beat_second
    assert "only one" in rank_options([(o("a"), clr(), 1.0)]).reason_first_beat_second


def test_ties_keep_listing_order_so_the_model_choice_wins_a_tie():
    r = rank_options([(o("first"), clr(), 1.0), (o("second"), clr(), 1.0)])
    assert r.options[0].option.assignments[0].spare_id == "first"


def test_greedy_prefers_reusing_one_spare_across_non_overlapping_blocks():
    b1, b2 = blk("b1", 1.0, 2.0), blk("b2", 4.0, 2.0)
    f = facts([b1, b2], [veh("veh-2"), veh("veh-3")], [opr("op-1")], ready_count=2)
    g = greedy_option([b1, b2], {"b1": ["veh-2", "veh-3"], "b2": ["veh-2", "veh-3"]}, {}, f, POL, T0)
    assert g is not None and {a.spare_id for a in g.assignments} == {"veh-2"}


def test_greedy_needs_two_spares_for_overlapping_blocks_and_none_when_last_spare_would_go():
    b1, b2 = blk("b1", 1.0, 3.0, op="op-1"), blk("b2", 2.0, 3.0, op="op-2")
    ops = [opr("op-1"), opr("op-2")]
    sp = {"b1": ["veh-2", "veh-3"], "b2": ["veh-2", "veh-3"]}
    f = facts([b1, b2], [veh("veh-2"), veh("veh-3")], ops, ready_count=3)
    g = greedy_option([b1, b2], sp, {}, f, POL, T0)
    assert g is not None and {a.spare_id for a in g.assignments} == {"veh-2", "veh-3"}
    f2 = facts([b1, b2], [veh("veh-2"), veh("veh-3")], ops, ready_count=2)       # would leave 0 spares
    assert greedy_option([b1, b2], sp, {}, f2, POL, T0) is None


def test_greedy_swaps_operator_only_when_the_current_one_cannot_drive():
    b = blk("b1", op="op-1")
    f = facts([b], [veh("veh-2")], [opr("op-1", hours_today=9.0), opr("op-2")], ready_count=3)
    g = greedy_option([b], {"b1": ["veh-2"]}, {"b1": ["op-1", "op-2"]}, f, POL, T0)
    assert g is not None and g.assignments[0].operator_id == "op-2"


def test_rank_plans_exact_first_larger_as_runner_up_and_reasoned():
    art = {"trained_vehicle_types": ["standard", "articulated"],
           "training_valid_to": {"P": FAR, "standard": FAR, "articulated": FAR, "lift": FAR}}
    b = blk("b1", op="op-1")
    f = facts([b], [veh("veh-2", type="articulated"), veh("veh-3", type="standard")], [opr("op-1", **art)],
              ready_count=3)
    r = rank_plans([], [b], {"b1": ["veh-2", "veh-3"]}, {"b1": ["op-1"]}, f, POL, T0)
    assert [x.option.assignments[0].spare_id for x in r.top3] == ["veh-3", "veh-2"]
    assert r.top3[0].clearance.tier == "exact" and r.top3[1].clearance.tier == "larger"
    assert "exact vehicle type" in r.reason_first_beat_second


def test_soft_score_counts_depot_match():
    b = blk("b1")
    f = facts([b], [veh("veh-2")], [opr()])
    assert soft_score(Option((Assign("b1", "veh-2"),)), f) == 1.0
