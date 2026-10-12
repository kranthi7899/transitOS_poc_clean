"""Q4 (A180, A181): option building and ordering for the 'which instant is meant' escalation. Pure functions; the
options are judged by clearance (`safe_evaluate`), never by this module. Expected rows written from the spec."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from transitos.clearance.facts import Facts, native_profile
from transitos.contracts.objects import Block, Needs, Operator, Vehicle
from transitos.contracts.policy import AgencyPolicy
from transitos.workflow.ambiguity import needs_human_for, with_reading

NY = "America/New_York"
EARLY, LATE = "2026-11-01T05:00:00Z", "2026-11-01T06:00:00Z"
B0 = datetime(2026, 11, 1, 5, 0, tzinfo=UTC)              # the block starts in the ambiguous hour
FAR = datetime(2030, 1, 1, tzinfo=UTC)
POL = AgencyPolicy(agency="a")


class _Active(dict):
    def get(self, key, default=None):
        return "active"


def op(id_: str, **kw) -> Operator:
    base = {"licence_class": "B", "endorsements": ["P"], "trained_vehicle_types": ["standard"],
            "training_valid_to": {"P": FAR, "standard": FAR, "lift": FAR}, "lift_trained": True,
            "hours_today": 0.0, "hours_week": 20.0, "medical_valid_to": FAR,
            "available_from": datetime(2026, 11, 1, 6, tzinfo=UTC), "available_to": B0 + timedelta(hours=15)}
    return Operator(id=id_, **{**base, **kw})


def amb(o: Operator) -> dict:
    return {"product": "workforce", "subject": o.id, "field": "available_from", "key": None,
            "local": "2026-11-01T01:00:00", "timezone": NY, "readings": [EARLY, LATE], "read_as": LATE,
            "record": o.model_dump(mode="json")}


def world():
    b = Block(id="b1", vehicle_id="veh-1", operator_id="op-a", start=B0, end=B0 + timedelta(hours=4),
              start_depot="North", needs=Needs(seats=30, wheelchair_positions=1, lift=True, type="standard"))
    v = Vehicle(id="veh-2", agency="a", seats=40, wheelchair_positions=2, lift_ok=True, type="standard",
                status="ready", depot="North")
    f = Facts(blocks={"b1": b}, vehicles={"veh-2": v}, operators={}, ready_count=3, commitments={},
              profiles={"scheduling": native_profile("scheduling"), "workforce": native_profile("workforce")},
              trigger_vehicle_id="veh-1", asset_status=_Active(), ready_unknown=0)
    return f


def test_options_are_ordered_by_fewest_open_items_then_id_and_name_what_is_left():
    a = op("op-a", last_duty_end=B0 - timedelta(hours=1))      # 1 h rest: a warn that needs acknowledging
    b = op("op-b")
    (e,) = needs_human_for([amb(a), amb(b)], ["b1"], {"b1": set()}, {"b1": ["veh-2"]}, world(), POL, B0)
    assert [(o["operator"], o["reading"]) for o in e["options"]] == [("op-b", EARLY), ("op-a", EARLY)]
    assert e["options"][1]["still_to_acknowledge"] == ["operator_rest"] and e["options"][1]["warnings"] == 1
    assert "still to acknowledge: operator_rest" in e["options"][1]["text"]
    assert e["options"][0]["text"] == "op-b if 01:00 means 05:00Z: on spare veh-2 for b1; nothing else to acknowledge"
    assert [s["id"] for s in e["subjects"]] == ["op-a", "op-b"] and "no_option" not in e


def test_an_eligible_operator_is_no_subject_and_no_spare_is_said():
    a, b = op("op-a"), op("op-b")
    (e,) = needs_human_for([amb(a), amb(b)], ["b1"], {"b1": {"op-a"}}, {"b1": []}, world(), POL, B0)
    assert [s["id"] for s in e["subjects"]] == ["op-b"]
    assert e["options"] == []
    assert e["no_option"] == ("No candidate would be feasible for b1 under either reading (no spare vehicle is "
                              "feasible for it either)")


def test_infeasible_under_both_readings_is_said_explicitly():
    a = op("op-a", medical_valid_to=B0 - timedelta(days=1))   # expired: no reading makes it feasible
    (e,) = needs_human_for([amb(a)], ["b1"], {"b1": set()}, {"b1": ["veh-2"]}, world(), POL, B0)
    assert e["options"] == [] and e["no_option"] == "No candidate would be feasible for b1 under either reading"


def test_with_reading_changes_one_field_only():
    o = op("op-a", last_duty_end=B0)
    t = datetime(2026, 11, 1, 5, tzinfo=UTC)
    n = with_reading(o, "available_from", None, t)
    assert n.available_from == t and n.model_dump(exclude={"available_from"}) == o.model_dump(exclude={"available_from"})
    assert with_reading(o, "training_valid_to", "P", t).training_valid_to == {**o.training_valid_to, "P": t}
    with pytest.raises(ValueError):
        with_reading(o, "unknown_fields", None, t)
