"""CrosswalkService with the resolver, through the real path (bus -> service -> crosswalk store -> outbox -> bus).

Exact native id valid at the event's own time = fast path (existing agencies unchanged). Otherwise the resolver
decides from the identity evidence the adapter put in the event (native_vin / native_plate / native_name):
MATCH -> events.resolved; AMBIGUOUS -> parked + events.failed listing the candidates; NO_MATCH -> parked +
events.failed as before; an exception inside the resolver -> parked + events.failed (fail closed, never dropped).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from transitos.contracts.crosswalk import CrosswalkEntry
from transitos.contracts.envelope import TOPIC_FAILED, TOPIC_RAW, TOPIC_RESOLVED, CloudEvent
from transitos.data.identifiers import make_vin
from transitos.plumbing import CrosswalkService

from .conftest import AG

S = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
VIN_1 = make_vin("0ZB", "B1C2D", 2024, "Z", 1)
VIN_2 = make_vin("0ZB", "B1C2D", 2024, "Z", 2)


def fault(eid, native, at, **data) -> CloudEvent:
    return CloudEvent(id=eid, source=f"adapter/telematics/{AG}", type="VehicleFaultDetected", time=at, agencyid=AG,
                      data={"native_vehicle_id": native, "spn": 1, "fmi": 1, **data})


def seed(xw) -> None:
    for e in (CrosswalkEntry(agency=AG, product="scheduling", native_id="BUS-44", entity="vehicle",
                             canonical_id="veh-1", vin=VIN_1, plate="ZZ 4KTR219", display_name="Bus 44"),
              CrosswalkEntry(agency=AG, product="scheduling", native_id="BUS-45", entity="vehicle",
                             canonical_id="veh-2", vin=VIN_2, plate="ZZ 5ABC103", display_name="Bus 45"),
              CrosswalkEntry(agency=AG, product="telematics", native_id="T-77", entity="vehicle",
                             canonical_id="veh-1", valid_to=S),
              CrosswalkEntry(agency=AG, product="telematics", native_id="T-90", entity="vehicle",
                             canonical_id="veh-1", valid_from=S),
              CrosswalkEntry(agency=AG, product="maintenance", native_id="1106", entity="asset",
                             canonical_id="veh-1")):
        xw.resolve(e)


def run(bus, svc, *events):
    for e in events:
        bus.send(TOPIC_RAW, "k", e)
    svc.drain()
    return ({m[2].id: m[2] for m in bus.messages(TOPIC_RESOLVED)}, {m[2].id: m[2] for m in bus.messages(TOPIC_FAILED)})


def test_fast_path_uses_the_events_own_time(bus, svc, xw):
    seed(xw)
    ok, failed = run(bus, svc, fault("before", "T-77", S - timedelta(seconds=1)), fault("at", "T-90", S),
                     fault("old-after", "T-77", S))
    assert ok["before"].canonicalid == "veh-1" and ok["at"].canonicalid == "veh-1"
    assert "old-after" in failed and "old-after" not in ok
    assert ok["at"].data["refs"]["telematics"] == "T-90"            # the current unit
    assert "identity" not in ok["before"].data                       # fast path payload unchanged


def test_resolver_match_by_vin_resolves_and_records_the_evidence(bus, svc, xw):
    seed(xw)
    ok, failed = run(bus, svc, fault("v", "T-500", S, native_vin=VIN_2.lower()))
    assert ok["v"].canonicalid == "veh-2" and not failed
    ident = ok["v"].data["identity"]
    assert ident["outcome"] == "MATCH" and ident["confidence"] == 1.0 and any("vin" in x for x in ident["evidence"])


def test_dirty_name_resolves_to_the_same_vehicle_not_a_second_one(bus, svc, xw):
    """Review Focus 2 through the service: every dirty spelling lands on veh-1."""
    seed(xw)
    names = ["bus 44", "BUS-044", "BUS-44 ", "Bus O44"]
    ok, failed = run(bus, svc, *[fault(f"n{i}", f"T-6{i}", S, native_name=n) for i, n in enumerate(names)])
    assert {ok[f"n{i}"].canonicalid for i in range(len(names))} == {"veh-1"} and not failed


def test_ambiguous_is_parked_with_candidates_and_reported(bus, svc, xw):
    seed(xw)
    ok, failed = run(bus, svc, fault("a", "T-400", S, native_name="Bus 4"))
    assert not ok
    assert failed["a"].failurereason.startswith("ambiguous")
    assert "veh-1" in failed["a"].failurereason and "veh-2" in failed["a"].failurereason
    [u] = xw.unresolved(AG)
    assert u["native_id"] == "T-400" and u["resolution"]["outcome"] == "AMBIGUOUS"
    assert [c["canonical_id"] for c in u["resolution"]["candidates"]] == ["veh-1", "veh-2"]


def test_lone_candidate_below_threshold_is_parked_for_a_human(bus, svc, xw):
    """Review Focus 3 through the service: one fuzzy candidate (< 0.9) never auto-resolves."""
    xw.resolve(CrosswalkEntry(agency=AG, product="scheduling", native_id="BUS-44", entity="vehicle",
                              canonical_id="veh-1", display_name="Bus 44"))
    ok, failed = run(bus, svc, fault("l", "T-400", S, native_name="Bus 4"))
    assert not ok and failed["l"].failurereason.startswith("ambiguous")
    [u] = xw.unresolved(AG)
    assert len(u["resolution"]["candidates"]) == 1 and u["resolution"]["candidates"][0]["score"] < 0.9


def test_no_match_is_parked_as_before(bus, svc, xw):
    seed(xw)
    ok, failed = run(bus, svc, fault("n", "T-999", S))
    assert not ok and failed["n"].failurereason == "unresolved vehicle telematics:T-999"
    assert xw.unresolved(AG)[0]["resolution"]["outcome"] == "NO_MATCH"


def test_resolver_exception_parks_the_event_fail_closed(db, bus, xw):
    seed(xw)

    class Boom:
        def resolve_detailed(self, *a, **k):
            raise RuntimeError("boom")

    svc = CrosswalkService(db, bus, xw, resolver=Boom())
    ok, failed = run(bus, svc, fault("x", "T-500", S, native_vin=VIN_1))
    assert not ok and "resolver_error" in failed["x"].failurereason and "boom" in failed["x"].failurereason
    assert [u["event_id"] for u in xw.unresolved(AG)] == ["x"]


def test_human_pick_republishes_and_the_event_resolves(bus, svc, xw):
    seed(xw)
    run(bus, svc, fault("a", "T-400", S, native_name="Bus 4"))
    assert svc.apply_mapping(CrosswalkEntry(agency=AG, product="telematics", native_id="T-400", entity="vehicle",
                                            canonical_id="veh-2")) == 1
    ok, failed = run(bus, svc)
    assert ok["a"].canonicalid == "veh-2" and list(failed) == ["a"] and not xw.unresolved(AG)


def test_a_republished_event_still_outside_validity_is_parked_again_not_dropped(bus, svc, xw):
    seed(xw)
    run(bus, svc, fault("late", "T-77", S + timedelta(hours=1)))
    # a human maps the old unit again, but only for a window that ends before the event
    svc.apply_mapping(CrosswalkEntry(agency=AG, product="telematics", native_id="T-77", entity="vehicle",
                                     canonical_id="veh-1", valid_from=S - timedelta(days=9), valid_to=S))
    ok, _ = run(bus, svc)
    assert "late" not in ok
    assert [u["event_id"] for u in xw.unresolved(AG)] == ["late"]


def test_operator_events_stay_exact_only(bus, svc, xw):
    xw.resolve(CrosswalkEntry(agency=AG, product="workforce", native_id="OP-310", entity="operator",
                              canonical_id="op-1", display_name="OP-310"))
    e = CloudEvent(id="o", source=f"adapter/workforce/{AG}", type="OperatorAvailabilityChanged", time=S,
                   agencyid=AG, data={"native_operator_id": "OP-0310", "status": "sick"})
    ok, failed = run(bus, svc, e)
    assert not ok and failed["o"].failurereason == "unresolved operator workforce:OP-0310"
