"""Step 3.1 ground truth + day script. Expected rows are written from the Step 3 spec text:
bus 3 coolant fault (SPN 110) 09:40, clears 10:15, returns to service 11:30; each beat exactly once, in order."""
from datetime import UTC, datetime, timedelta

import pytest

from transitos.mocks.clock import SimClock
from transitos.world.script import Beat, DayScript
from transitos.world.truth import GroundTruth

DAY = datetime(2026, 10, 10, tzinfo=UTC)


def at(h, m=0, s=0):
    return DAY + timedelta(hours=h, minutes=m, seconds=s)


def test_example_coolant_fault_rows_from_the_spec():
    sc = DayScript.example_coolant_fault(DAY)
    rows = [(b.at, b.vehicle, b.kind) for b in sc.beats]
    assert rows == [(at(9, 40), "veh-3", "fault_on"), (at(10, 15), "veh-3", "fault_off"),
                    (at(11, 30), "veh-3", "return_to_service")]
    assert sc.beats[0].params["spn"] == 110          # coolant temperature


def test_beat_is_frozen_and_validated():
    b = Beat(at(1), "veh-1", "dvir", {"defect_text": "x"})
    with pytest.raises(Exception):
        b.kind = "fault_on"                           # frozen dataclass
    with pytest.raises(ValueError):
        Beat(at(1), "veh-1", "explode", {})
    with pytest.raises(ValueError):
        Beat(datetime(2026, 10, 10, 9, 40), "veh-1", "fault_on", {})   # naive time: never allowed


def test_each_beat_exactly_once_in_order_even_if_called_repeatedly_or_earlier():
    gt = GroundTruth(SimClock(at(6)), DayScript.example_coolant_fault(DAY), seed=1)
    assert gt.advance_to(at(9, 39, 59)) == []
    first = gt.advance_to(at(10, 20))                 # fault_on and fault_off, in order
    assert [b.kind for b in first] == ["fault_on", "fault_off"]
    assert gt.advance_to(at(10, 20)) == []            # repeated: nothing again
    assert gt.advance_to(at(9, 0)) == []              # earlier than the last call: nothing, nothing re-emitted
    assert [b.kind for b in gt.advance_to(at(23))] == ["return_to_service"]
    assert gt.advance_to(at(23, 59)) == []


def test_beats_out_of_input_order_are_emitted_in_time_order_ties_stable():
    beats = (Beat(at(8), "veh-2", "fault_on", {"spn": 168, "fmi": 1}), Beat(at(7), "veh-1", "dvir", {}),
             Beat(at(8), "veh-1", "fault_on", {"spn": 110, "fmi": 0}))
    gt = GroundTruth(SimClock(at(6)), DayScript("agency-a", beats), seed=0)
    out = gt.advance_to(at(9))
    assert [(b.at, b.vehicle) for b in out] == [(at(7), "veh-1"), (at(8), "veh-2"), (at(8), "veh-1")]


def test_state_follows_the_example_day():
    gt = GroundTruth(SimClock(at(6)), DayScript.example_coolant_fault(DAY), seed=1)
    seen = {}
    for label, t in [("before", at(9, 39, 59)), ("fault", at(9, 40)), ("cleared", at(10, 15)),
                     ("still_held", at(11, 29, 59)), ("returned", at(11, 30))]:
        gt.advance_to(t)
        seen[label] = gt.state("veh-3")
    assert seen == {"before": "ok", "fault": "faulted", "cleared": "out_of_service",
                    "still_held": "out_of_service", "returned": "ok"}
    assert gt.state("veh-1") == "ok"                  # an untouched vehicle


def test_intermittent_fault_not_withdrawn_goes_back_to_ok_when_it_clears():
    s = DayScript("agency-a", (Beat(at(8), "veh-1", "fault_on", {"spn": 168, "fmi": 1, "withdrawn": False}),
                               Beat(at(8, 6), "veh-1", "fault_off", {"spn": 168, "fmi": 1})))
    gt = GroundTruth(SimClock(at(6)), s, seed=0)
    gt.advance_to(at(8, 1))
    assert gt.state("veh-1") == "faulted"
    gt.advance_to(at(8, 6))
    assert gt.state("veh-1") == "ok"


def test_script_serialisation_is_byte_identical_and_roundtrips():
    a, b = DayScript.example_coolant_fault(DAY), DayScript.example_coolant_fault(DAY)
    assert a.to_json() == b.to_json()
    assert DayScript.from_json(a.to_json()) == a
