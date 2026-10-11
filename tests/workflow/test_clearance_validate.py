"""`validate_output` and the stub/fallback assignment go through clearance (one rule implementation)."""
from __future__ import annotations

from test_clearance_plan import block, spares_of, start, veh, world_with  # noqa: F401  (shared builders)

from transitos.contracts.objects import Operator
from transitos.workflow.reasoning import (
    StubReasoner,
    code_ranked_fallback,
    validate_output,
)


class Spy(StubReasoner):
    name = "spy"

    def __init__(self):
        self.facts = None

    def propose(self, facts, attempt=0):
        self.facts = facts
        return super().propose(facts, attempt)


def facts_for(make_env, vehicles, blocks, operators=None):
    spy = Spy()
    env = make_env(world=world_with(vehicles, operators, blocks), reasoner=spy)
    start(env)
    return spy.facts


def out(*assignments, decision="pull_now"):
    return {"decision": decision, "assignments": [
        {"block_id": b, "spare_id": s, "operator_id": o} for b, s, o in assignments]}


def test_validator_accepts_one_spare_on_two_non_overlapping_blocks(make_env):
    f = facts_for(make_env, [veh("veh-1"), veh("veh-2"), veh("veh-3")],
                  [block("B1", 1, 3), block("B2", 5, 7, op="OP-2")])
    _, errs = validate_output(out(("B1", "veh-2", None), ("B2", "veh-2", None)), f)
    assert errs == []


def test_validator_rejects_one_spare_on_two_overlapping_blocks(make_env):
    f = facts_for(make_env, [veh("veh-1"), veh("veh-2"), veh("veh-3"), veh("veh-4")],
                  [block("B1", 1, 5), block("B2", 3, 7, op="OP-2")])
    _, errs = validate_output(out(("B1", "veh-2", None), ("B2", "veh-2", None)), f)
    assert any(e.startswith("clearance:double_booking") for e in errs)


def test_validator_applies_last_spare_to_distinct_spares(make_env):
    f = facts_for(make_env, [veh("veh-1"), veh("veh-2"), veh("veh-3")],
                  [block("B1", 1, 3), block("B2", 5, 7, op="OP-2")])
    _, errs = validate_output(out(("B1", "veh-2", None), ("B2", "veh-3", None)), f)
    assert any(e.startswith("clearance:last_spare") for e in errs)       # 2 ready - 2 distinct = 0 < 1


def test_validator_checks_the_operator_against_the_type_of_the_assigned_vehicle(make_env):
    ops = [Operator(id="OP-1", qualifications=["standard"]), Operator(id="OP-2", qualifications=["standard"]),
           Operator(id="OP-3", qualifications=["standard", "articulated"])]
    f = facts_for(make_env, [veh("veh-1"), veh("veh-2", "articulated"), veh("veh-3", "articulated")],
                  [block("B1", 1, 5)], ops)
    _, errs = validate_output(out(("B1", "veh-2", None)), f)              # OP-1 is not qualified on articulated
    assert any("operator_eligible" in e or "operator_qualification" in e for e in errs)
    _, errs = validate_output(out(("B1", "veh-2", "OP-3")), f)
    assert errs == []


def test_stub_and_fallback_pick_a_valid_operator_for_the_larger_vehicle(make_env):
    ops = [Operator(id="OP-1", qualifications=["standard"]), Operator(id="OP-3", qualifications=["standard", "articulated"])]
    f = facts_for(make_env, [veh("veh-1"), veh("veh-2", "articulated"), veh("veh-3", "articulated")],
                  [block("B1", 1, 5)], ops)
    for raw in (StubReasoner().propose(f), code_ranked_fallback(f)):
        assert validate_output(raw, f)[1] == []
        assert raw["assignments"][0]["operator_id"] == "OP-3"


def test_validator_still_catches_structure_errors(make_env):
    f = facts_for(make_env, [veh("veh-1"), veh("veh-2"), veh("veh-3")], [block("B1", 1, 3)])
    _, errs = validate_output(out(("B1", "veh-99", None)), f)
    assert "spare_not_feasible:B1:veh-99" in errs
    _, errs = validate_output(out(), f)
    assert "missing_assignment:B1" in errs
    _, errs = validate_output(out(("B9", "veh-2", None)), f)
    assert "unknown_block:B9" in errs
