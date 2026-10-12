"""Clearance rules, one behaviour per test (Phase 10 Step 1, amendment A). Pure functions: no I/O."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from transitos.clearance import Option, evaluate_option, safe_evaluate
from transitos.clearance.evaluate import Assign, operator_items, vehicle_items
from transitos.clearance.facts import (
    SCHEDULING_FIELDS,
    WORKFORCE_FIELDS,
    CapabilityProfile,
    Facts,
    native_profile,
)
from transitos.contracts.objects import Block, Needs, Operator, Vehicle
from transitos.contracts.policy import AgencyPolicy, Limits

T0 = datetime(2026, 10, 10, 6, 0, tzinfo=UTC)
FAR = datetime(2030, 1, 1, tzinfo=UTC)


def veh(id="veh-2", type="standard", seats=40, wc=2, lift=True, status="ready", length=None) -> Vehicle:
    return Vehicle(id=id, agency="a", seats=seats, wheelchair_positions=wc, lift_ok=lift, type=type,
                   status=status, depot="North", length_ft=length)


def blk(id="b1", start_h=1.0, hours=4.0, op="op-1", veh_id="veh-1", **needs) -> Block:
    n = {"seats": 30, "wheelchair_positions": 1, "lift": True, "type": "standard", **needs}
    return Block(id=id, vehicle_id=veh_id, operator_id=op, start=T0 + timedelta(hours=start_h),
                 end=T0 + timedelta(hours=start_h + hours), start_depot="North", needs=Needs(**n))


def opr(id="op-1", **kw) -> Operator:
    base = {"licence_class": "B", "endorsements": ["P"], "trained_vehicle_types": ["standard"],
            "training_valid_to": {"P": FAR, "standard": FAR, "lift": FAR}, "lift_trained": True,
            "hours_today": 4.0, "hours_week": 20.0, "medical_valid_to": FAR, "available_from": T0 - timedelta(hours=1),
            "available_to": T0 + timedelta(hours=15)}
    base.update(kw)
    return Operator(id=id, **base)


def facts(blocks=(), vehicles=(), operators=(), ready_count=3, commitments=None, drop_workforce=(),
          drop_sched=(), no_workforce_profile=False, legacy=False, asset_status="ACTIVE") -> Facts:
    asset_status = _all_active() if asset_status == "ACTIVE" else asset_status
    profs = {"scheduling": native_profile("scheduling", drop=drop_sched)}
    if not no_workforce_profile:
        drop = tuple(drop_workforce)
        if legacy:
            drop += ("licence_class", "endorsements", "trained_vehicle_types", "training_valid_to")
        profs["workforce"] = native_profile("workforce", drop=drop)
    return Facts(blocks={b.id: b for b in blocks}, vehicles={v.id: v for v in vehicles},
                 operators={o.id: o for o in operators}, ready_count=ready_count,
                 commitments={} if commitments is None else commitments, profiles=profs,
                 trigger_vehicle_id="veh-1",
                 asset_status=asset_status, ready_unknown=0)   # R5-2: explicit (tests that need >0 set it)   # S2 F4: the default sees every vehicle's asset as active


class _AllActive(dict):
    """S2 F4: an asset-status feed in which every vehicle's maintenance asset is active (tests that do not care)."""

    def get(self, key, default=None):
        return "active"


def _all_active():
    return _AllActive()


POL = AgencyPolicy(agency="a")


def by_rule(items, rule):
    return [i for i in items if i.rule == rule]


def one(items, rule):
    got = by_rule(items, rule)
    assert len(got) == 1, (rule, [i.model_dump() for i in items])
    return got[0]


# ---- profiles -------------------------------------------------------------------------------------
def test_profile_unlisted_field_is_unknown():
    p = CapabilityProfile("x", {"a": "native"})
    assert p.basis("a") == "native" and p.basis("b") == "unknown"


def test_native_profile_drop_marks_unknown():
    p = native_profile("workforce", drop=("hours_week",))
    assert p.basis("hours_week") == "unknown" and p.basis("hours_today") == "native"
    assert set(WORKFORCE_FIELDS) >= {"licence_class", "hours_week"} and "seats" in SCHEDULING_FIELDS


# ---- vehicle rules --------------------------------------------------------------------------------
def test_vehicle_exact_type_passes_clean():
    f = facts()
    items = vehicle_items(blk(), veh(), f, POL)
    assert not [i for i in items if i.status != "pass"]
    assert one(items, "vehicle_fit").status == "pass"


@pytest.mark.parametrize("field,value,rule", [
    ("status", "reserved", "vehicle_ready"), ("seats", 10, "vehicle_seats"),
    ("wc", 0, "vehicle_wheelchair"), ("lift", False, "vehicle_lift")])
def test_vehicle_known_shortfall_is_violation(field, value, rule):
    items = vehicle_items(blk(), veh(**{field: value}), facts(), POL)
    assert one(items, rule).status == "violation"


def test_larger_class_is_a_warning_that_needs_acknowledgement():
    it = one(vehicle_items(blk(), veh(type="articulated"), facts(), POL), "vehicle_fit")
    assert (it.status, it.requires_ack, it.basis) == ("warn", True, "native")


def test_smaller_class_is_never_a_candidate():
    it = one(vehicle_items(blk(type="standard"), veh(type="cutaway"), facts(), POL), "vehicle_fit")
    assert it.status == "violation"


def test_exact_setting_turns_larger_into_exclusion():
    pol = AgencyPolicy(agency="a", vehicle_fit="exact")
    assert one(vehicle_items(blk(), veh(type="articulated"), facts(), pol), "vehicle_fit").status == "violation"


def test_incomparable_types_are_excluded_not_guessed():
    it = one(vehicle_items(blk(type="bus"), veh(type="van"), facts(), POL), "vehicle_fit")
    assert it.status == "violation"


def test_block_without_type_requirement_has_no_fit_item():
    assert not by_rule(vehicle_items(blk(type=None), veh(), facts(), POL), "vehicle_fit")


def test_length_restriction_violated_by_native_length():
    items = vehicle_items(blk(max_vehicle_length_ft=45.0), veh(type="articulated", length=60.0), facts(), POL)
    it = one(items, "vehicle_length")
    assert (it.status, it.basis) == ("violation", "native")


def test_length_not_provided_is_unverifiable_never_a_typical_value():
    """Owner Q1: no class default stands in for a length the product did not provide, whatever the class."""
    for vtype in ("articulated", "standard", "cutaway"):
        it = one(vehicle_items(blk(max_vehicle_length_ft=30.0), veh(type=vtype), facts(), POL), "vehicle_length")
        assert (it.status, it.basis, it.requires_ack) == ("unverifiable", "unknown", True), vtype
        assert "length_ft" in it.detail          # the acknowledgement names the missing field


def test_length_provided_decides_even_for_a_short_class():
    over = one(vehicle_items(blk(max_vehicle_length_ft=30.0), veh(type="cutaway", length=33.0), facts(), POL),
               "vehicle_length")
    assert (over.status, over.basis) == ("violation", "native")
    ok = one(vehicle_items(blk(max_vehicle_length_ft=45.0), veh(type="articulated", length=44.0), facts(), POL),
             "vehicle_length")
    assert (ok.status, ok.basis) == ("pass", "native")


def test_length_unknown_type_is_unverifiable():
    it = one(vehicle_items(blk(max_vehicle_length_ft=45.0, type=None), veh(type="mystery"), facts(), POL),
             "vehicle_length")
    assert (it.status, it.basis, it.requires_ack) == ("unverifiable", "unknown", True)


def test_unsupplied_capacity_fields_are_unverifiable_not_pass_not_violation():
    f = facts(drop_sched=("seats", "wheelchair_positions", "lift_ok"))
    items = vehicle_items(blk(), veh(seats=1, wc=0, lift=False), f, POL)
    for rule in ("vehicle_seats", "vehicle_wheelchair", "vehicle_lift"):
        it = one(items, rule)
        assert (it.status, it.basis, it.requires_ack) == ("unverifiable", "unknown", True)


# ---- operator rules: hours ------------------------------------------------------------------------
def test_daily_hours_cap_is_inclusive_at_the_limit():
    b = blk(hours=4.0)
    assert one(operator_items(b, opr(hours_today=6.0), "standard", facts(), POL), "operator_hours_day").status == "pass"
    assert one(operator_items(b, opr(hours_today=6.1), "standard", facts(), POL),
               "operator_hours_day").status == "violation"


def test_weekly_cap_uses_eight_day_window_limit():
    b = blk(hours=4.0)
    assert one(operator_items(b, opr(hours_week=66.0), "standard", facts(), POL), "operator_hours_week").status == "pass"
    it = one(operator_items(b, opr(hours_week=66.5), "standard", facts(), POL), "operator_hours_week")
    assert it.status == "violation" and "70" in it.detail


def test_weekly_cap_limit_comes_from_policy():
    pol = AgencyPolicy(agency="a", limits=Limits(max_operator_hours_week=40.0))
    assert one(operator_items(blk(), opr(hours_week=37.0), "standard", facts(), pol),
               "operator_hours_week").status == "violation"


def test_weekly_hours_unsupplied_is_unverifiable():
    f = facts(drop_workforce=("hours_week",))
    it = one(operator_items(blk(), opr(hours_week=999.0), "standard", f, POL), "operator_hours_week")
    assert (it.status, it.basis, it.requires_ack) == ("unverifiable", "unknown", True)


def test_weekly_hours_unsupplied_but_today_alone_breaks_cap_is_derived_violation():
    pol = AgencyPolicy(agency="a", limits=Limits(max_operator_hours_week=8.0, max_operator_hours_day=20.0))
    f = facts(drop_workforce=("hours_week",))
    it = one(operator_items(blk(hours=4.0), opr(hours_today=6.0), "standard", f, pol), "operator_hours_week")
    assert (it.status, it.basis) == ("violation", "derived")


def test_daily_hours_unsupplied_is_unverifiable():
    f = facts(drop_workforce=("hours_today",))
    assert one(operator_items(blk(), opr(hours_today=99.0), "standard", f, POL),
               "operator_hours_day").status == "unverifiable"


# ---- operator rules: qualification ----------------------------------------------------------------
def test_fully_qualified_operator_has_no_problem_items():
    items = operator_items(blk(), opr(), "standard", facts(), POL)
    assert not [i for i in items if i.status not in ("pass", "not_evaluated")], [i.model_dump() for i in items]


def test_licence_class_too_low_is_violation():
    it = one(operator_items(blk(), opr(licence_class="C"), "standard", facts(), POL), "operator_licence_class")
    assert it.status == "violation"
    assert one(operator_items(blk(), opr(licence_class="A"), "standard", facts(), POL),
               "operator_licence_class").status == "pass"


def test_missing_passenger_endorsement_is_violation():
    it = one(operator_items(blk(), opr(endorsements=[]), "standard", facts(), POL), "operator_endorsement")
    assert it.status == "violation"


def test_untrained_vehicle_type_is_violation():
    it = one(operator_items(blk(), opr(trained_vehicle_types=["cutaway"]), "standard", facts(), POL),
             "operator_vehicle_training")
    assert it.status == "violation"


def test_lift_training_missing_is_violation_only_when_block_needs_lift():
    assert one(operator_items(blk(), opr(lift_trained=False), "standard", facts(), POL),
               "operator_lift_training").status == "violation"
    assert not by_rule(operator_items(blk(lift=False), opr(lift_trained=False), "standard", facts(), POL),
                       "operator_lift_training")


@pytest.mark.parametrize("cred", ["P", "standard", "lift"])
def test_expiry_is_a_violation_on_the_day_it_expires_and_after_but_not_the_day_before(cred):
    """Review Focus 1: a credential whose valid_to is day D fails a block on day D (and later), not D-1."""
    day = datetime(2026, 10, 10, tzinfo=UTC)
    tv = {"P": FAR, "standard": FAR, "lift": FAR, cred: day}
    status = lambda d: one(operator_items(blk(), opr(training_valid_to=tv), "standard", facts(), POL, ),
                           "operator_training_expiry").status
    # block runs on 2026-10-10 (T0 is 06:00 on the 10th): the day it expires
    assert status(day) == "violation"
    tv[cred] = day + timedelta(days=1)       # expires the day after the block: still valid
    assert status(day) == "pass"
    tv[cred] = day - timedelta(days=1)       # expired the day before
    assert status(day) == "violation"


def test_missing_expiry_date_for_a_held_credential_is_unverifiable():
    it = one(operator_items(blk(), opr(training_valid_to={}), "standard", facts(), POL), "operator_training_expiry")
    assert (it.status, it.basis, it.requires_ack) == ("unverifiable", "unknown", True)


def test_new_qualification_fields_unsupplied_is_unverifiable_not_violation():
    f = facts(drop_workforce=("licence_class", "endorsements", "trained_vehicle_types", "training_valid_to",
                              "qualifications"))
    items = operator_items(blk(), opr(licence_class=None, endorsements=[], trained_vehicle_types=[],
                                      training_valid_to={}), "standard", f, POL)
    q = [i for i in items if i.rule.startswith("operator_") and i.rule in (
        "operator_licence_class", "operator_endorsement", "operator_vehicle_training", "operator_qualification")]
    assert q and all(i.status == "unverifiable" and i.basis == "unknown" and i.requires_ack for i in q)


def test_legacy_qualifications_used_only_when_new_fields_unsupplied():
    f = facts(legacy=True)
    ok = operator_items(blk(), Operator(id="op-1", qualifications=["standard"], lift_trained=True), "standard", f, POL)
    assert one(ok, "operator_qualification").status == "pass"
    bad = operator_items(blk(), Operator(id="op-1", qualifications=["van"], lift_trained=True), "standard", f, POL)
    assert one(bad, "operator_qualification").status == "violation"
    assert not by_rule(ok, "operator_licence_class")


def test_new_fields_win_over_legacy_when_both_supplied():
    f = facts()
    o = opr(trained_vehicle_types=["cutaway"], qualifications=["standard"])
    assert one(operator_items(blk(), o, "standard", f, POL), "operator_vehicle_training").status == "violation"


def test_qualification_for_larger_vehicle_is_judged_on_the_vehicles_type():
    items = operator_items(blk(), opr(), "articulated", facts(), POL)
    assert one(items, "operator_vehicle_training").status == "violation"      # not trained on articulated
    ok = operator_items(blk(), opr(trained_vehicle_types=["standard", "articulated"],
                                   training_valid_to={"P": FAR, "standard": FAR, "articulated": FAR, "lift": FAR}),
                        "articulated", facts(), POL)
    assert one(ok, "operator_vehicle_training").status == "pass"


# ---- operator rules: availability and optional ----------------------------------------------------
def test_availability_window_known_breach_is_violation():
    o = opr(available_from=T0 + timedelta(hours=2))
    assert one(operator_items(blk(), o, "standard", facts(), POL), "operator_availability").status == "violation"
    o2 = opr(available_to=T0 + timedelta(hours=3))
    assert one(operator_items(blk(), o2, "standard", facts(), POL), "operator_availability").status == "violation"


def test_availability_unsupplied_is_unverifiable():
    f = facts(drop_workforce=("availability",))
    assert one(operator_items(blk(), opr(), "standard", f, POL), "operator_availability").status == "unverifiable"


def test_rest_and_spread_not_evaluated_without_data_and_warn_on_known_breach():
    items = operator_items(blk(), opr(), "standard", facts(), POL)
    for rule in ("operator_rest", "operator_spread"):
        it = one(items, rule)
        assert (it.status, it.basis, it.requires_ack) == ("not_evaluated", "unknown", False)
    o = opr(last_duty_end=T0 - timedelta(hours=2), duty_start_today=T0 - timedelta(hours=12))
    items = operator_items(blk(), o, "standard", facts(), POL)
    assert one(items, "operator_rest").status == "warn" and one(items, "operator_rest").requires_ack
    assert one(items, "operator_spread").status == "warn"
    ok = opr(last_duty_end=T0 - timedelta(hours=20), duty_start_today=T0 - timedelta(hours=1))
    assert one(operator_items(blk(), ok, "standard", facts(), POL), "operator_rest").status == "pass"


# (The Step 1 "missing data is never a violation" property test is replaced by the real sweep in
# tests/clearance/test_missing_data_sweep.py and tests/integration/test_missing_data_sweep.py; F7.)


# ---- option-level ---------------------------------------------------------------------------------
def two_blocks_one_spare(second_start_h: float):
    b1, b2 = blk("b1", 1.0, 2.0, op="op-1"), blk("b2", second_start_h, 2.0, op="op-2")
    f = facts([b1, b2], [veh("veh-2")], [opr("op-1"), opr("op-2")], ready_count=2)
    return f, Option((Assign("b1", "veh-2"), Assign("b2", "veh-2")))


def test_spare_reused_across_non_overlapping_blocks_is_one_spare_consumed():
    f, opt = two_blocks_one_spare(3.5)
    c = evaluate_option(opt, f, POL, T0)
    assert not c.excluded, [i.model_dump() for i in c.items if i.status == "violation"]
    ls = one(c.items, "last_spare")
    assert ls.status == "pass" and "1 distinct" in ls.detail


def test_spare_reused_across_overlapping_blocks_is_a_violation():
    f, opt = two_blocks_one_spare(2.0)
    c = evaluate_option(opt, f, POL, T0)
    assert c.excluded and any(i.rule == "double_booking" and i.status == "violation" for i in c.items)


def test_last_spare_counts_distinct_spares():
    b1, b2 = blk("b1", 1.0, 2.0), blk("b2", 4.0, 2.0)
    f = facts([b1, b2], [veh("veh-2"), veh("veh-3")], [opr("op-1")], ready_count=2)
    one_spare = evaluate_option(Option((Assign("b1", "veh-2"), Assign("b2", "veh-2"))), f, POL, T0)
    two_spares = evaluate_option(Option((Assign("b1", "veh-2"), Assign("b2", "veh-3"))), f, POL, T0)
    assert not one_spare.excluded                       # 2 ready - 1 used = 1 >= min_spares 1
    assert two_spares.excluded and one(two_spares.items, "last_spare").status == "violation"


def test_min_distinct_spares_is_exact_where_greedy_overcounts():
    """F6: b1 may take A or B, b2 only B, the blocks do not overlap: B serves both, so ONE spare is needed.
    The greedy pass takes A for b1 (listed first) and then needs B as well (two)."""
    from transitos.clearance.rules import count_distinct_spares, min_distinct_spares
    b1, b2 = blk("b1", 1.0, 2.0), blk("b2", 4.0, 2.0)
    assert min_distinct_spares([b1, b2], {"b1": ["A", "B"], "b2": ["B"]}) == 1
    assert count_distinct_spares([b1, b2], {"b1": ["A", "B"], "b2": ["B"]}).exact


def test_min_distinct_spares_overlapping_blocks_need_two_and_a_block_without_candidates_is_none():
    from transitos.clearance.rules import min_distinct_spares
    b1, b2 = blk("b1", 1.0, 3.0), blk("b2", 2.0, 3.0)
    assert min_distinct_spares([b1, b2], {"b1": ["A", "B"], "b2": ["A", "B"]}) == 2
    assert min_distinct_spares([b1, b2], {"b1": ["A"], "b2": ["A"]}) is None      # no assignment exists
    assert min_distinct_spares([b1, b2], {"b1": ["A"], "b2": []}) is None


def test_min_distinct_spares_past_the_search_cap_is_the_greedy_upper_bound_flagged_not_exact():
    """F6 / A127: past the cap the count falls back to the greedy UPPER bound (fail closed: it can only
    overstate the spares needed) and says it is not exact."""
    from transitos.clearance.rules import count_distinct_spares
    b1, b2 = blk("b1", 1.0, 2.0), blk("b2", 4.0, 2.0)
    r = count_distinct_spares([b1, b2], {"b1": ["A", "B"], "b2": ["B"]}, max_nodes=1)
    assert (r.n, r.exact) == (2, False)


def test_min_distinct_spares_eight_sequential_blocks_one_spare_is_fast_and_exact():
    from transitos.clearance.rules import count_distinct_spares
    bs = [blk(f"b{k}", 2.0 * k, 2.0) for k in range(8)]
    r = count_distinct_spares(bs, {b.id: ["A", "B", "C"] for b in bs})
    assert (r.n, r.exact) == (1, True)


def test_last_spare_unknown_ready_count_is_unverifiable():
    f = facts([blk()], [veh("veh-2")], [opr()], ready_count=None)
    c = evaluate_option(Option((Assign("b1", "veh-2"),)), f, POL, T0)
    it = one(c.items, "last_spare")
    assert (it.status, it.requires_ack) == ("unverifiable", True) and not c.excluded


def test_operator_overlap_across_assignments_is_double_booking():
    b1, b2 = blk("b1", 1.0, 3.0, op=None), blk("b2", 2.0, 3.0, op=None)
    f = facts([b1, b2], [veh("veh-2"), veh("veh-3")], [opr("op-1")], ready_count=5)
    c = evaluate_option(Option((Assign("b1", "veh-2", "op-1"), Assign("b2", "veh-3", "op-1"))), f, POL, T0)
    assert c.excluded and any(i.rule == "double_booking" for i in c.items)


def test_operator_hours_accumulate_over_several_blocks():
    b1, b2 = blk("b1", 1.0, 3.0, op=None), blk("b2", 5.0, 3.0, op=None)
    f = facts([b1, b2], [veh("veh-2")], [opr("op-1", hours_today=4.5)], ready_count=5)
    c = evaluate_option(Option((Assign("b1", "veh-2", "op-1"), Assign("b2", "veh-2", "op-1"))), f, POL, T0)
    assert c.excluded and any(i.rule == "operator_hours_day" and i.status == "violation" for i in c.items)


def test_commitments_overlap_is_double_booking_and_unknown_commitments_fail_closed():
    b = blk("b1", 1.0, 2.0)
    other = (T0 + timedelta(hours=2), T0 + timedelta(hours=4), "other-block")
    f = facts([b], [veh("veh-2")], [opr()], ready_count=5, commitments={"operator:op-1": [other]})
    c = evaluate_option(Option((Assign("b1", "veh-2", "op-1"),)), f, POL, T0)
    assert c.excluded
    f.commitments = None
    c = evaluate_option(Option((Assign("b1", "veh-2"),)), f, POL, T0)
    it = one(c.items, "double_booking")
    assert c.excluded and it.status == "violation" and it.basis == "unknown"


def test_larger_tier_flagged_and_counted():
    b = blk()
    f = facts([b], [veh("veh-2", type="articulated")],
              [opr(trained_vehicle_types=["standard", "articulated"],
                   training_valid_to={"P": FAR, "standard": FAR, "articulated": FAR, "lift": FAR})], ready_count=3)
    c = evaluate_option(Option((Assign("b1", "veh-2"),)), f, POL, T0)
    assert not c.excluded and c.tier == "larger" and c.warnings == 1
    assert any(i.rule == "vehicle_fit" and i.requires_ack for i in c.items)


def test_current_operator_failing_for_the_assigned_vehicles_type_excludes_the_option():
    f = facts([blk()], [veh("veh-2", type="articulated")], [opr()], ready_count=3)
    c = evaluate_option(Option((Assign("b1", "veh-2"),)), f, POL, T0)
    assert c.excluded and any(i.rule == "operator_vehicle_training" and i.status == "violation" for i in c.items)


def test_safe_evaluate_fails_closed_on_any_exception():
    f = facts([blk()], [veh("veh-2")], [opr()])
    c = safe_evaluate(Option((Assign("b1", "no-such-vehicle"),)), f, POL, T0)
    assert c.excluded
    assert [i.rule for i in c.items] == ["clearance_error"] and c.items[0].status == "violation"
    class Boom(Facts):                                   # an injected bug inside the module
        @property
        def tables(self):
            raise RuntimeError("boom")
    g = facts([blk()], [veh("veh-2")], [opr()])
    g.__class__ = Boom
    assert safe_evaluate(Option((Assign("b1", "veh-2"),)), g, POL, T0).excluded


# ---- R4-1: last_spare with counted spares whose maintenance status is unknown -------------------------------
def test_probe_r4b_two_unreachable_spares_make_last_spare_unverifiable():
    """Reviewer repro (probe_r4b): S1 and S2 both 'unreachable', ready_count 2, min_spares 1, the plan uses S1.
    The remaining spare S2 may be retired: last_spare must not be a native pass."""
    from transitos.clearance.rules import count_ready
    st = {"S1": "unreachable", "S2": "unreachable"}
    ready, unknown = count_ready(["S1", "S2"], st)
    assert (ready, unknown) == (2, 2)
    f = facts([blk()], [veh("S1"), veh("S2")], [opr()], ready_count=ready, asset_status=st)
    f.ready_unknown = unknown
    items = evaluate_option(Option((Assign("b1", "S1"),)), f, AgencyPolicy(agency="a", limits=Limits(min_spares=1)),
                            T0).items
    ls = one(items, "last_spare")
    assert (ls.status, ls.basis, ls.requires_ack) == ("unverifiable", "unknown", True)


@pytest.mark.parametrize("statuses,want", [
    ({"S1": "active", "S2": "active"}, (2, 0)), ({"S1": "active", "S2": "retired"}, (1, 0)),
    ({"S1": "unreachable", "S2": "retired"}, (1, 1)), ({"S1": "missing", "S2": ""}, (2, 2)),
    (None, (2, 2)),                                       # no feed at all: every counted spare is unknown
])
def test_count_ready_excludes_known_retired_and_counts_unknown(statuses, want):
    from transitos.clearance.rules import count_ready
    assert count_ready(["S1", "S2"], statuses) == want


# ---- bug sweep A2, R5-2 (A163): ready_unknown has no default (a caller that forgets it must not fail open) -----
def test_r5_2_facts_cannot_be_built_without_ready_unknown():
    with pytest.raises(TypeError, match="ready_unknown"):
        Facts(blocks={}, vehicles={}, operators={}, ready_count=3, commitments={})  # type: ignore[call-arg]


def test_r5_2_facts_from_a_dict_without_ready_unknown_is_refused():
    d = Facts(blocks={}, vehicles={}, operators={}, ready_count=3, commitments={}, ready_unknown=0).to_dict()
    d.pop("ready_unknown")
    with pytest.raises(KeyError):
        Facts.from_dict(d)
