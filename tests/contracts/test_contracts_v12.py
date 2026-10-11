"""Contract v1.2 (Phase 10; requests B-1, C-1, D-1): additive only, every new field optional."""
from datetime import UTC, datetime

from transitos.contracts.clearance import ClearanceItem
from transitos.contracts.crosswalk import Candidate, CrosswalkEntry, Resolution
from transitos.contracts.events import PMOverdueData, VehicleFaultDetectedData
from transitos.contracts.objects import (
    DecisionRecord,
    Needs,
    Operator,
    Plan,
    RunState,
    Vehicle,
)
from transitos.contracts.policy import AgencyPolicy, Limits

NOW = datetime(2026, 10, 11, tzinfo=UTC)


def test_old_payloads_still_validate_unchanged():
    assert Operator(id="op-1").licence_class is None
    assert Operator(id="op-1", qualifications=["standard"], lift_trained=True).endorsements == []
    assert Needs(seats=10).max_vehicle_length_ft is None
    assert Vehicle(id="v", agency="a", seats=10).length_ft is None
    assert Plan(decision="monitor").clearance == []
    assert RunState(run_id="r", agency="a", vehicle_id="v", trigger_event={}).acknowledged == []
    assert DecisionRecord(run_id="r", plan_version=1, trigger_event_id="e").acknowledged == []
    assert CrosswalkEntry(agency="a", product="p", native_id="n", entity="vehicle", canonical_id="veh-1").valid_to is None


def test_operator_qualification_and_limits_defaults():
    o = Operator(id="op-1", licence_class="B", endorsements=["P"], trained_vehicle_types=["standard"],
                 training_valid_to={"lift": NOW}, medical_valid_to=NOW)
    assert o.training_valid_to["lift"] == NOW
    lim = Limits()
    assert (lim.max_operator_hours_week, lim.min_rest_hours, lim.max_spread_hours) == (70.0, 8.0, 15.0)


def test_vehicle_fit_defaults_to_allow_larger():
    assert AgencyPolicy(agency="a").vehicle_fit == "allow_larger"
    assert AgencyPolicy(agency="a", vehicle_fit="exact").vehicle_fit == "exact"


def test_clearance_item_roundtrip_and_status_values():
    c = ClearanceItem(rule="vehicle_fit", status="warn", basis="native", detail="larger class", requires_ack=True)
    assert ClearanceItem.model_validate_json(c.model_dump_json()) == c
    p = Plan(decision="pull_now", clearance=[c], rank_reason="exact beat larger", vehicle_fit="allow_larger")
    assert Plan.model_validate_json(p.model_dump_json()).clearance[0].requires_ack is True


def test_resolution_outcome_and_candidates():
    r = Resolution(outcome="AMBIGUOUS", confidence=0.7,
                   candidates=[Candidate(canonical_id="veh-1", score=0.7, evidence=["name"]),
                               Candidate(canonical_id="veh-9", score=0.65, evidence=["name"])])
    assert [c.canonical_id for c in r.candidates] == ["veh-1", "veh-9"]


def test_event_identity_attributes_optional():
    d = VehicleFaultDetectedData(native_vehicle_id="T-77", spn=110, fmi=0, native_vin="1ZZ", native_plate="ZZ-1")
    assert d.native_vin == "1ZZ" and PMOverdueData(native_asset_id="1", service_code="PM", due_mi=1, meter_mi=2).native_vin is None


def test_per_row_unknown_fields_default_empty_and_roundtrip():
    """C-2: a record can say which of its fields were blank or absent in the source."""
    assert Operator(id="op-1").unknown_fields == []
    assert Vehicle(id="v", agency="a", seats=1).unknown_fields == []
    assert Needs().unknown_fields == []
    o = Operator(id="op-1", unknown_fields=["hours_today", "lift_trained"])
    assert Operator.model_validate_json(o.model_dump_json()).unknown_fields == ["hours_today", "lift_trained"]
