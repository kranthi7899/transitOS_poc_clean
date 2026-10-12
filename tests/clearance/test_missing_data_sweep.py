"""F7 sweep (replaces the Step 1 'property' test, which only re-checked items that already said basis unknown).

For EVERY rule and EVERY input field it reads, starting from a record on which that rule passes cleanly on native
data, the field is made missing three ways and the rule must answer `unverifiable` (safety/legal) or
`not_evaluated` (optional), never `pass` and never `violation`:
  * dropped  - the product does not supply the field at all (capability profile: unknown)
  * default  - the record holds the contract's default value for the field and lists it in `unknown_fields`
               (what a read adapter produces for a blank cell or an absent key; C-2)
  * value=x  - the record holds an arbitrary value x (one that would pass, one that would violate) and lists
               the field as unknown: the rule must not read it
Blank cells through the REAL mock products are swept in tests/integration/test_missing_data_sweep.py.

Documented exceptions (each from the policy text): a missing vehicle length falls back to the class default
tagged `assumed` (amendment A Q3 resolution order, A66; F9 keeps a breach on it excluding); double-booking
without a schedule feed fails closed (plan table); vehicle_ready and kill_switch have no missing mode."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from transitos.clearance.evaluate import (
    Assign,
    Option,
    noteworthy,
    safe_evaluate,
)
from transitos.clearance.facts import (
    RECORD_FIELDS,
    Facts,
    Tables,
    load_tables,
    native_profile,
)
from transitos.contracts.objects import Block, Needs, Operator, Vehicle
from transitos.contracts.policy import AgencyPolicy

T0 = datetime(2026, 10, 10, 6, 0, tzinfo=UTC)
FAR = datetime(2030, 1, 1, tzinfo=UTC)
PAST = datetime(2026, 1, 1, tzinfo=UTC)
POL = AgencyPolicy(agency="a")
OPTIONAL = {"operator_rest", "operator_spread"}
LEGACY_DROP = ("licence_class", "endorsements", "trained_vehicle_types", "training_valid_to")


def base_block(unknown=(), **needs) -> Block:
    n = {"seats": 30, "wheelchair_positions": 1, "lift": True, "type": "standard", "max_vehicle_length_ft": 45.0,
         **needs, "unknown_fields": list(unknown)}
    return Block(id="b1", vehicle_id="veh-1", operator_id="op-1", start=T0 + timedelta(hours=1),
                 end=T0 + timedelta(hours=5), start_depot="North", needs=Needs(**n))


def base_vehicle(**kw) -> Vehicle:
    return Vehicle(**{"id": "veh-2", "agency": "a", "seats": 40, "wheelchair_positions": 2, "lift_ok": True,
                      "type": "standard", "status": "ready", "depot": "North", "length_ft": 40.0, **kw})


def base_operator(**kw) -> Operator:
    return Operator(**{"id": "op-1", "licence_class": "B", "endorsements": ["P"], "trained_vehicle_types": ["standard"],
                       "training_valid_to": {"P": FAR, "standard": FAR, "lift": FAR}, "lift_trained": True,
                       "hours_today": 4.0, "hours_week": 20.0, "available_from": T0 - timedelta(hours=1),
                       "available_to": T0 + timedelta(hours=15), "medical_valid_to": FAR,
                       "last_duty_end": T0 - timedelta(hours=12), "duty_start_today": T0 - timedelta(hours=2), **kw})


# (rule, record, contract field, capability-profile field or None, values that would otherwise pass / violate)
CASES = [
    ("operator_hours_day", "operator", "hours_today", "hours_today", [0.0, 9.5]),
    ("operator_hours_week", "operator", "hours_week", "hours_week", [0.0, 69.0]),
    ("operator_licence_class", "operator", "licence_class", "licence_class", [None, "C", "A"]),
    ("operator_endorsement", "operator", "endorsements", "endorsements", [[], ["P"]]),
    ("operator_vehicle_training", "operator", "trained_vehicle_types", "trained_vehicle_types", [[], ["standard"]]),
    ("operator_training_expiry", "operator", "training_valid_to", "training_valid_to",
     [{}, {"P": PAST, "standard": PAST, "lift": PAST}, {"P": FAR, "standard": FAR, "lift": FAR}]),
    ("operator_training_expiry", "operator", "endorsements", "endorsements", [[], ["P"]]),
    ("operator_training_expiry", "operator", "trained_vehicle_types", "trained_vehicle_types", [[], ["standard"]]),
    ("operator_training_expiry", "operator", "lift_trained", "lift_trained", [False, True]),
    ("operator_lift_training", "operator", "lift_trained", "lift_trained", [False, True]),
    ("operator_availability", "operator", "available_from", "availability", [None, T0 + timedelta(hours=3)]),
    ("operator_availability", "operator", "available_to", "availability", [None, T0 + timedelta(hours=2)]),
    ("operator_medical", "operator", "medical_valid_to", "medical_valid_to", [None, PAST, FAR]),
    ("operator_rest", "operator", "last_duty_end", "duty_times", [None, T0 - timedelta(hours=1)]),
    ("operator_spread", "operator", "duty_start_today", "duty_times", [None, T0 - timedelta(hours=14)]),
    ("vehicle_seats", "vehicle", "seats", "seats", [0, 100]),
    ("vehicle_wheelchair", "vehicle", "wheelchair_positions", "wheelchair_positions", [0, 9]),
    ("vehicle_lift", "vehicle", "lift_ok", "lift_ok", [False, True]),
    ("vehicle_fit", "vehicle", "type", "type", ["bus", "standard", "cutaway", "articulated"]),
    # R2-1: the operator rules that depend on the vehicle's type
    *[(r, "vehicle", "type", "type", ["bus", "standard", "cutaway", "articulated"])
      for r in ("operator_licence_class", "operator_vehicle_training", "operator_training_expiry")],
    ("vehicle_length", "vehicle", "length_ft", "length_ft", [None, 10.0, 99.0]),          # not provided -> unverifiable (Q1)
    ("vehicle_seats", "needs", "seats", None, [0, 100]),
    ("vehicle_wheelchair", "needs", "wheelchair_positions", None, [0, 9]),
    ("vehicle_lift", "needs", "lift", None, [False, True]),
    ("operator_lift_training", "needs", "lift", None, [False, True]),
    ("operator_training_expiry", "needs", "lift", None, [False, True]),
    ("vehicle_fit", "needs", "type", None, [None, "standard", "articulated"]),
    ("vehicle_length", "needs", "max_vehicle_length_ft", None, [None, 10.0, 99.0]),
]
DEFAULTS = {"operator": Operator, "vehicle": Vehicle, "needs": Needs}


def contract_default(record: str, field: str):
    f = DEFAULTS[record].model_fields[field]
    if f.default_factory is not None:
        return f.default_factory()
    return 0 if f.is_required() else f.default      # Vehicle.seats has no default: the adapters' placeholder is 0


def facts_for(drop_profile: str | None, legacy: bool = False) -> Facts:
    wf_drop = (LEGACY_DROP if legacy else ()) + ((drop_profile,) if drop_profile else ())
    return Facts(blocks={}, vehicles={}, operators={}, ready_count=3, commitments={}, ready_unknown=0,
                 profiles={"scheduling": native_profile("scheduling", drop=(drop_profile,) if drop_profile else ()),
                           "workforce": native_profile("workforce", drop=wf_drop)})


def judge(rule, block, vehicle, op, facts, asset_status="ACTIVE"):
    """Through the planner's own path (R2-1): one block onto one spare, the block keeps its operator, so the
    operator rules see the vehicle type exactly as evaluate_option hands it over."""
    f = Facts(blocks={block.id: block}, vehicles={vehicle.id: vehicle}, operators={op.id: op},
              ready_count=facts.ready_count, commitments=facts.commitments, profiles=facts.profiles,
              ready_unknown=facts.ready_unknown,
              asset_status={vehicle.id: "active"} if asset_status == "ACTIVE" else asset_status)
    items = safe_evaluate(Option((Assign(block.id, vehicle.id),)), f, POL, T0).items
    return [i for i in items if i.rule == rule]


# S2 F4: judge() gives rows that do not care about the asset feed an active asset ("ACTIVE")


@pytest.mark.parametrize("variant,feed", [("dropped (no asset-status feed)", None),
                                          ("missing (no maintenance asset)", {}),
                                          ("unreachable (maintenance did not answer)", {"veh-2": "unreachable"}),
                                          ("blank status", {"veh-2": ""}),
                                          ("unrecognised status", {"veh-2": "in_shop?"})])
def test_asset_status_missing_is_unverifiable_never_pass_or_violation(variant, feed):
    """S2 F4: 'maintenance asset status' as a dropped / blank input of the safety rule asset_active."""
    assert judge("asset_active", base_block(), base_vehicle(), base_operator(), facts_for(None))[0].status == "pass"
    got = judge("asset_active", base_block(), base_vehicle(), base_operator(), facts_for(None), asset_status=feed)
    assert got and all((i.status, i.basis, i.requires_ack) == ("unverifiable", "unknown", True) for i in got), \
        [i.model_dump() for i in got]


def variants(case):
    _, record, field, cap, values = case
    out = [("default", contract_default(record, field))] + [(f"value={v!r}", v) for v in values]
    if cap is not None:
        out.insert(0, ("dropped", None))
    return [(case, name, v) for name, v in out]


ALL = [v for c in CASES for v in variants(c)]


def test_every_case_starts_from_a_clean_native_pass():
    """The sweep is only meaningful if, with full data, each rule passes natively on the base records."""
    for rule, *_ in CASES:
        got = judge(rule, base_block(), base_vehicle(), base_operator(), facts_for(None))
        assert got and all((i.status, i.basis) == ("pass", "native") for i in got), (rule, got)


@pytest.mark.parametrize("case,variant,value", ALL,
                         ids=[f"{c[0]}<-{c[1]}.{c[2]}:{n}" for c, n, _ in ALL])
def test_missing_input_is_never_a_pass_and_never_a_violation(case, variant, value):
    rule, record, field, cap, _ = case
    block, vehicle, op = base_block(), base_vehicle(), base_operator()
    if variant == "dropped":
        facts = facts_for(cap)
    else:
        facts = facts_for(None)
        if record == "operator":
            op = base_operator(**{field: value, "unknown_fields": [field]})
        elif record == "vehicle":
            vehicle = base_vehicle(**{field: value, "unknown_fields": [field]})
        else:
            block = base_block(unknown=[field], **{field: value})
    got = judge(rule, block, vehicle, op, facts)
    assert got, f"{rule} reported nothing when {record}.{field} was missing ({variant})"
    assert all(noteworthy(i) for i in got)     # F3: never hidden from the plan/decision record as a clean pass
    want = "not_evaluated" if rule in OPTIONAL else "unverifiable"
    assert all(i.status == want and i.basis == "unknown" for i in got), [i.model_dump() for i in got]
    assert all(i.requires_ack == (want == "unverifiable") for i in got)


@pytest.mark.parametrize("variant,value", [("dropped", None), ("default", []), ("value=['van']", ["van"]),
                                           ("value=['standard']", ["standard"])])
def test_legacy_qualification_list_missing_is_unverifiable(variant, value):
    """A legacy-only product (none of the new qualification fields): the legacy list is the input."""
    op = base_operator(**({} if variant == "dropped" else {"qualifications": value, "unknown_fields": ["qualifications"]}))
    facts = facts_for("qualifications" if variant == "dropped" else None, legacy=True)
    assert judge("operator_qualification", base_block(), base_vehicle(),
                 base_operator(qualifications=["standard"]), facts_for(None, legacy=True))[0].status == "pass"
    got = judge("operator_qualification", base_block(), base_vehicle(), op, facts)
    assert got and all((i.status, i.basis) == ("unverifiable", "unknown") for i in got)


def test_every_record_field_the_rules_read_is_swept():
    """No input left out: every workforce/scheduling capability field and every need is in CASES."""
    swept = {(r, f) for _, r, f, _, _ in CASES}
    op_fields = {f for cap in ("hours_today", "hours_week", "licence_class", "endorsements", "trained_vehicle_types",
                               "training_valid_to", "lift_trained", "availability", "medical_valid_to", "duty_times")
                 for f in RECORD_FIELDS.get(cap, (cap,))}
    assert {("operator", f) for f in op_fields} <= swept
    assert {("vehicle", f) for f in ("seats", "wheelchair_positions", "lift_ok", "type", "length_ft")} <= swept
    assert {("needs", f) for f in ("seats", "wheelchair_positions", "lift", "type", "max_vehicle_length_ft")} <= swept


# ---- option-level inputs ---------------------------------------------------------------------------------
def _option_facts(**kw) -> tuple[Facts, Option]:
    b1 = base_block()
    b2 = Block(id="b2", vehicle_id="veh-1", operator_id="op-2", start=T0 + timedelta(hours=6),
               end=T0 + timedelta(hours=10), start_depot="North", needs=b1.needs)
    f = Facts(blocks={"b1": b1, "b2": b2}, vehicles={"veh-2": base_vehicle()},
              operators={"op-1": kw.pop("op1", base_operator()), "op-2": base_operator(id="op-2")},
              ready_count=kw.pop("ready", 3), commitments=kw.pop("commitments", {}), ready_unknown=0,
              profiles={"scheduling": native_profile("scheduling"), "workforce": native_profile("workforce")})
    return f, Option((Assign("b1", "veh-2"), Assign("b2", "veh-2", "op-1")))


@pytest.mark.parametrize("variant,statuses", [("unreachable", {"veh-2": "unreachable", "veh-3": "unreachable"}),
                                              ("missing", {"veh-2": "active", "veh-3": "missing"}),
                                              ("blank", {"veh-2": "active", "veh-3": ""}),
                                              ("no feed", None)])
def test_counted_spare_asset_status_missing_makes_last_spare_unverifiable(variant, statuses):
    """R4-1 sweep: a spare counted toward ready_count whose maintenance status is unknown -> last_spare is
    unverifiable (basis unknown), never a native pass."""
    from transitos.clearance.rules import count_ready
    ready, unknown = count_ready(["veh-2", "veh-3"], statuses)
    f, opt = _option_facts(ready=ready)
    f.ready_unknown = unknown
    got = [i for i in safe_evaluate(opt, f, POL, T0).items if i.rule == "last_spare"]
    assert got and all((i.status, i.basis, i.requires_ack) == ("unverifiable", "unknown", True) for i in got), \
        [i.model_dump() for i in got]


def test_ready_count_missing_is_unverifiable():
    f, opt = _option_facts(ready=None)
    got = [i for i in safe_evaluate(opt, f, POL, T0).items if i.rule == "last_spare"]
    assert got and all((i.status, i.basis) == ("unverifiable", "unknown") for i in got)


def test_schedule_feed_missing_fails_closed_the_documented_exception():
    f, opt = _option_facts(commitments=None)
    got = [i for i in safe_evaluate(opt, f, POL, T0).items if i.rule == "double_booking"]
    assert got and all((i.status, i.basis) == ("violation", "unknown") for i in got)


def test_f12_option_that_moves_an_operator_off_a_block_still_fails_closed_without_the_feed():
    """Bug sweep A (F12, A150): ignoring the commitments an option replaces needs the feed; with no feed the
    option that moves op-1 off b1 onto an overlapping b2 is still a fail-closed violation, never a pass."""
    b1 = base_block()
    b2 = Block(id="b2", vehicle_id="veh-5", operator_id="op-2", start=b1.start + timedelta(hours=1),
               end=b1.end + timedelta(hours=1), start_depot="North", needs=b1.needs)
    f = Facts(blocks={"b1": b1, "b2": b2}, vehicles={"veh-2": base_vehicle(), "veh-3": base_vehicle(id="veh-3")},
              operators={"op-1": base_operator(), "op-2": base_operator(id="op-2"), "op-3": base_operator(id="op-3")},
              ready_count=4, commitments=None, ready_unknown=0,
              profiles={"scheduling": native_profile("scheduling"), "workforce": native_profile("workforce")})
    opt = Option((Assign("b1", "veh-2", "op-3"), Assign("b2", "veh-3", "op-1")))
    got = [i for i in safe_evaluate(opt, f, POL, T0).items if i.rule == "double_booking"]
    assert got and all((i.status, i.basis) == ("violation", "unknown") for i in got)
    assert safe_evaluate(opt, f, POL, T0).excluded


@pytest.mark.parametrize("field,value", [("hours_today", 0.0), ("hours_today", 9.9), ("hours_week", 0.0),
                                         ("hours_week", 69.9)])
def test_accumulated_hours_with_a_missing_input_are_never_a_pass_or_violation(field, value):
    """Option level (F5): an operator kept on b1 and given b2 whose hours are unknown is unverifiable, never judged
    on a placeholder."""
    f, opt = _option_facts(op1=base_operator(**{field: value, "unknown_fields": [field]}))
    rule = "operator_hours_day" if field == "hours_today" else "operator_hours_week"
    got = [i for i in safe_evaluate(opt, f, POL, T0).items if i.rule == rule
           and ("op-1" in i.detail or i.status == "unverifiable")]
    assert got and all((i.status, i.basis) == ("unverifiable", "unknown") for i in got), [i.model_dump() for i in got]


@pytest.mark.parametrize("variant,value", [("dropped", None), ("default", "bus"), ("value='bus'", "bus"),
                                           ("value='standard'", "standard"), ("value='van'", "van")])
def test_legacy_qualification_with_the_vehicle_type_missing_is_unverifiable(variant, value):
    """R2-1: legacy-only product, the vehicle's type is the missing input. Operators listing 'standard' (the real
    type) and 'bus' (the adapters' placeholder) must both be unverifiable, never a violation or a pass."""
    vehicle = base_vehicle() if variant == "dropped" else base_vehicle(type=value, unknown_fields=["type"])
    facts = facts_for("type" if variant == "dropped" else None, legacy=True)
    for quals in (["standard"], ["bus"], ["van"]):
        got = judge("operator_qualification", base_block(), vehicle, base_operator(qualifications=quals), facts)
        assert got and all((i.status, i.basis) == ("unverifiable", "unknown") for i in got), (quals, got)


# ---- R3-1: with the vehicle's type unknown, ONLY the type-dependent checks are unverifiable ------------------
TYPE_UNKNOWN = [("dropped", None), ("default", "bus"), ("value='bus'", "bus"), ("value='standard'", "standard"),
                ("value='cutaway'", "cutaway"), ("value='articulated'", "articulated")]
TYPE_DEPENDENT = ("operator_licence_class", "operator_vehicle_training", "operator_training_expiry")


def _type_unknown(variant, value, **op):
    vehicle = base_vehicle() if variant == "dropped" else base_vehicle(type=value, unknown_fields=["type"])
    facts = facts_for("type" if variant == "dropped" else None)
    f = Facts(blocks={"b1": base_block()}, vehicles={vehicle.id: vehicle}, operators={"op-1": base_operator(**op)},
              ready_count=3, commitments={}, profiles=facts.profiles, ready_unknown=0)
    items = safe_evaluate(Option((Assign("b1", vehicle.id),)), f, POL, T0).items
    return {r: [(i.status, i.basis) for i in items if i.rule == r] for r in {i.rule for i in items}}


@pytest.mark.parametrize("variant,value", TYPE_UNKNOWN)
def test_type_unknown_exact_statuses_on_valid_data(variant, value):
    got = _type_unknown(variant, value)
    for r in TYPE_DEPENDENT:
        assert got[r] == [("unverifiable", "unknown")], (r, got[r])
    for r in ("operator_endorsement", "operator_lift_training", "operator_medical", "operator_hours_day",
              "operator_hours_week", "operator_availability"):
        assert got[r] == [("pass", "native")], (r, got[r])


@pytest.mark.parametrize("variant,value", TYPE_UNKNOWN)
@pytest.mark.parametrize("op,rule", [
    ({"training_valid_to": {"P": FAR, "standard": FAR, "lift": PAST}}, "operator_training_expiry"),
    ({"training_valid_to": {"P": PAST, "standard": FAR, "lift": FAR}}, "operator_training_expiry"),
    ({"endorsements": []}, "operator_endorsement"),
    ({"lift_trained": False}, "operator_lift_training"),
    ({"medical_valid_to": PAST}, "operator_medical"),
], ids=["lift_expired", "P_expired", "P_missing", "lift_untrained", "medical_expired"])
def test_type_unknown_known_type_independent_breaches_are_violations(variant, value, op, rule):
    """R3-1: a known expired lift or passenger credential, a missing P (every configured type needs it), missing
    lift training on a lift block, an expired medical card: violations (excluded), never acknowledgeable."""
    assert _type_unknown(variant, value, **op)[rule] == [("violation", "native")]


def test_type_unknown_endorsement_follows_the_configured_types_not_a_constant():
    """A136: P is judged without the type only because EVERY configured type needs it; add a type that does not
    and the endorsement check becomes unverifiable when the type is unknown."""
    t = load_tables()
    mixed = Tables(t.licence_order, t.passenger_endorsement_code,
                   {**t.vehicle_requirements, "van": {"min_licence_class": "C", "passenger_endorsement": False,
                                                      "training_type": "van"}},
                   t.vehicle_order)
    vehicle = base_vehicle(type="bus", unknown_fields=["type"])
    f = Facts(blocks={"b1": base_block()}, vehicles={vehicle.id: vehicle},
              operators={"op-1": base_operator(endorsements=[])}, ready_count=3, commitments={},
              profiles=facts_for(None).profiles, tables=mixed, ready_unknown=0)
    items = safe_evaluate(Option((Assign("b1", vehicle.id),)), f, POL, T0).items
    assert [(i.status, i.basis) for i in items if i.rule == "operator_endorsement"] == [("unverifiable", "unknown")]
