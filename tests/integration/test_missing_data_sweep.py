"""F7 sweep through the REAL mock products and read adapters (the canonical-level sweep is
tests/clearance/test_missing_data_sweep.py). For every input the rules read from the workforce export, the
scheduling vehicle payload and the block needs, the column/key is DROPPED and, separately, the cell is BLANKED
for the record under test; the rules that read it must answer `unverifiable` (safety/legal) or `not_evaluated`
(optional), never `pass` and never `violation`. Each case first checks the rule passes natively on full data.

Not swept here (covered at canonical level, with the reason): the legacy `qualifications` list (agency-042
exports licence codes there, so the legacy rule is not a clean pass on full data), the vehicle `length_ft`
(no agency-042 block declares a length limit; its missing value falls back to the ASSUMED class default)."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from transitos.clearance.evaluate import Assign, Option, safe_evaluate
from transitos.clearance.facts import CapabilityProfile, Facts
from transitos.contracts.policy import AgencyPolicy

from .conftest import AG, H
from .test_clearance_vs_mocks import make_env

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")
T5, T7 = datetime(2026, 1, 5, 5, 0, tzinfo=UTC), datetime(2026, 1, 5, 7, 0, tzinfo=UTC)
BLOCK, OP, OP_NATIVE, SPARE, SPARE_NATIVE = "BLK-1-1", "op-1", "OP-310", "veh-10", "BUS-53"
OPTIONAL = {"operator_rest", "operator_spread"}

WORKFORCE = {
    "hours_today": ["operator_hours_day"],
    "hours_week": ["operator_hours_week"],
    "licence_class": ["operator_licence_class"],
    "endorsements": ["operator_endorsement", "operator_training_expiry"],
    "trained_vehicle_types": ["operator_vehicle_training", "operator_training_expiry"],
    "training_valid_to": ["operator_training_expiry"],
    "lift_trained": ["operator_lift_training", "operator_training_expiry"],
    "available_from": ["operator_availability"],
    "available_to": ["operator_availability"],
    "medical_valid_to": ["operator_medical"],
    "last_duty_end": ["operator_rest"],
    "duty_start_today": ["operator_spread"],
}
VEHICLE = {"seats": ["vehicle_seats"], "wheelchair_positions": ["vehicle_wheelchair"], "lift_ok": ["vehicle_lift"],
           "type": ["vehicle_fit", "operator_licence_class", "operator_vehicle_training",
                    "operator_training_expiry"]}   # R2-1/R3-1: the operator rules that depend on the vehicle's type
NEEDS = {"seats": ["vehicle_seats"], "wheelchair_positions": ["vehicle_wheelchair"],
         "lift": ["vehicle_lift", "operator_lift_training", "operator_training_expiry"], "type": ["vehicle_fit"],
         "max_vehicle_length_ft": ["vehicle_length"]}
CASES = ([("workforce", f, how) for f in WORKFORCE for how in ("drop", "blank")]
         + [("vehicles", f, how) for f in VEHICLE for how in ("drop", "blank")]
         + [("needs", f, how) for f in NEEDS for how in ("drop", "blank")])


@pytest.fixture
def env(mocks):
    e = make_env(mocks)
    # give the operator known duty times so rest and spread are evaluated (and pass) on full data
    e.c("workforce").post("/sim/operator", headers=H, json={"operator": OP_NATIVE,
                                                            "last_duty_end": "2026-01-04T17:00:00Z",
                                                            "duty_start_today": "2026-01-05T04:00:00Z"})
    return e


def judge(env, from_export: bool = False):
    """`from_export`: take the operator straight from the workforce export, not from the eligible list (which
    already drops an operator with a KNOWN violation), to judge it on the planner's option path."""
    block = next(b for b in env.tools.list_blocks(AG, T5, T7).value if b.id == BLOCK)
    vehicle = env.tools.get_vehicle(AG, SPARE).value
    ops = env.tools._optional("operators")[0](AG) if from_export else env.tools.list_eligible_operators(AG, block).value
    op = next(o for o in ops if o.id == OP)
    profiles = {k: CapabilityProfile.from_dict(v) for k, v in env.tools.capability_profiles(AG).value.items()}
    facts = Facts(blocks={block.id: block}, vehicles={vehicle.id: vehicle}, operators={op.id: op}, ready_count=3,
                  commitments={}, profiles=profiles, ready_unknown=0)
    # the planner's own path (R2-1): the block keeps its operator, the operator rules get the type as handed over
    return safe_evaluate(Option((Assign(block.id, vehicle.id),)), facts, AgencyPolicy(agency=AG), T5).items


def rules_for(kind, field):
    return {"workforce": WORKFORCE, "vehicles": VEHICLE, "needs": NEEDS}[kind][field]


def make_missing(env, kind, field, how):
    if kind == "workforce":
        body = {"drop": [field]} if how == "drop" else {"blank": [field], "blank_for": [OP_NATIVE]}
        env.c("workforce").post("/sim/operator-fields", headers=H, json=body).raise_for_status()
    else:
        body = {kind: {how: [field]}, "ids": [SPARE_NATIVE if kind == "vehicles" else BLOCK]}
        env.c("scheduling").post("/sim/fields", headers=H, json=body).raise_for_status()


@pytest.mark.parametrize("kind,field,how", CASES, ids=[f"{k}.{f}:{h}" for k, f, h in CASES])
def test_missing_source_data_is_never_a_pass_and_never_a_violation(env, kind, field, how):
    rules = rules_for(kind, field)
    before = judge(env)
    for rule in rules:
        got = [i for i in before if i.rule == rule]
        if (kind, field) == ("needs", "max_vehicle_length_ft"):
            assert got == []                          # declared: no limit (null) -> no length rule on full data
        else:
            assert got and all((i.status, i.basis) == ("pass", "native") for i in got), (rule, got)
    make_missing(env, kind, field, how)
    after = judge(env)
    for rule in rules:
        got = [i for i in after if i.rule == rule]
        want = "not_evaluated" if rule in OPTIONAL else "unverifiable"
        assert got, f"{rule} said nothing when {kind}.{field} was {how}ped/blanked"
        assert all((i.status, i.basis) == (want, "unknown") for i in got), [i.model_dump() for i in got]


@pytest.mark.parametrize("how", ["drop", "blank"])
def test_vehicle_type_missing_judges_type_independent_checks_exactly(env, how):
    """R3-1 through the real products: the spare's type dropped or blanked. Endorsement and lift training are
    still judged (pass); the type-dependent checks are unverifiable; then OP-310's lift credential is set to
    yesterday and the expiry check becomes a VIOLATION, not an acknowledgeable unverifiable."""
    make_missing(env, "vehicles", "type", how)
    got = {r: {(i.status, i.basis) for i in judge(env) if i.rule == r} for r in (
        "operator_endorsement", "operator_lift_training", "operator_licence_class", "operator_vehicle_training",
        "operator_training_expiry")}
    assert got["operator_endorsement"] == got["operator_lift_training"] == {("pass", "native")}
    assert got["operator_licence_class"] == got["operator_vehicle_training"] == {("unverifiable", "unknown")}
    assert got["operator_training_expiry"] == {("unverifiable", "unknown")}
    env.c("workforce").post("/sim/operator", headers=H, json={"operator": OP_NATIVE, "training_valid_to":
        "P=2030-01-01T00:00:00Z;cutaway=2030-01-01T00:00:00Z;standard=2030-01-01T00:00:00Z;"
        "articulated=2030-01-01T00:00:00Z;lift=2026-01-04T00:00:00Z"})
    block = next(b for b in env.tools.list_blocks(AG, T5, T7).value if b.id == BLOCK)
    assert OP not in {o.id for o in env.tools.list_eligible_operators(AG, block).value}   # excluded up front
    assert {(i.status, i.basis) for i in judge(env, from_export=True)
            if i.rule == "operator_training_expiry"} == {("violation", "native")}
