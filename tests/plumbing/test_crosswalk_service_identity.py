"""CrosswalkService with the resolver, through the real path (bus -> service -> crosswalk store -> outbox -> bus).

Exact native id valid at the event's own time = fast path (existing agencies unchanged). Otherwise the resolver
decides from the identity evidence the adapter put in the event (native_vin / native_plate / native_name):
MATCH -> events.resolved; AMBIGUOUS -> parked + events.failed listing the candidates; NO_MATCH -> parked +
events.failed as before; an exception inside the resolver -> parked + events.failed (fail closed, never dropped).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from transitos.contracts.crosswalk import CrosswalkEntry
from transitos.contracts.envelope import (
    TOPIC_FAILED,
    TOPIC_RAW,
    TOPIC_RESOLVED,
    CloudEvent,
)
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
    names = ["bus 44", "BUS-044", "BUS-44 ", "bus 0044"]   # F3: a prefix O is no longer a zero
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


# ---------------------------------------------------------------- fix round (R2)
def test_f1_fast_path_with_contradicting_vin_and_plate_is_parked_as_ambiguous(bus, svc, xw):
    """F1: T-90 maps to veh-1, but the event carries veh-2's VIN and plate: a human decides, never veh-1."""
    seed(xw)
    ok, failed = run(bus, svc, fault("c", "T-90", S + timedelta(hours=1), native_vin=VIN_2,
                                     native_plate="ZZ 5ABC103", native_name="Bus 45"))
    assert not ok and failed["c"].failurereason.startswith("ambiguous")
    [u] = xw.unresolved(AG)
    assert sorted(c["canonical_id"] for c in u["resolution"]["candidates"]) == ["veh-1", "veh-2"]
    assert any("vin" in " ".join(c["evidence"]) for c in u["resolution"]["candidates"])


def test_f1_a_contradicting_vin_alone_is_enough(bus, svc, xw):
    seed(xw)
    ok, failed = run(bus, svc, fault("v", "T-90", S + timedelta(hours=1), native_vin=VIN_2))
    assert not ok and failed["v"].failurereason.startswith("ambiguous")


def test_f1_agreeing_missing_dirty_or_name_only_evidence_keeps_the_fast_path(bus, svc, xw):
    seed(xw)
    bad_vin2 = VIN_2[:8] + ("1" if VIN_2[8] != "1" else "2") + VIN_2[9:]
    at = S + timedelta(hours=1)
    ok, failed = run(bus, svc, fault("agree", "T-90", at, native_vin=VIN_1, native_plate="zz 4ktr219"),
                     fault("none", "T-90", at, native_vin="", native_plate=" "),
                     fault("dirty", "T-90", at, native_vin=bad_vin2),
                     fault("name", "T-90", at, native_name="Bus 45"))   # a stale name never overrides the id
    assert {k: e.canonicalid for k, e in ok.items()} == {"agree": "veh-1", "none": "veh-1", "dirty": "veh-1",
                                                         "name": "veh-1"} and not failed


def test_f8_overlapping_valid_rows_for_one_native_id_are_ambiguous_in_the_service(bus, svc, xw):
    """m9: two rows for T-77 valid at the same time: never 'the first vehicle'."""
    xw.resolve(CrosswalkEntry(agency=AG, product="telematics", native_id="T-77", entity="vehicle",
                              canonical_id="veh-1"))
    xw.resolve(CrosswalkEntry(agency=AG, product="telematics", native_id="T-77", entity="vehicle",
                              canonical_id="veh-5", valid_from=S))
    ok, failed = run(bus, svc, fault("o", "T-77", S + timedelta(hours=1)))
    assert not ok and "veh-1" in failed["o"].failurereason and "veh-5" in failed["o"].failurereason
    assert failed["o"].failurereason.startswith("ambiguous")


def test_f5_f8_candidates_without_a_match_are_kept_in_failed_and_stored(bus, svc, xw):
    """F5: top 0.73 with a 0.6 runner-up (lead > 0.1, below 0.9) is AMBIGUOUS, not NO_MATCH; m15: events.failed
    and the stored resolution keep every candidate with its evidence."""
    for n, name in ((1, "Bus 44"), (2, "Bus 47X")):
        xw.resolve(CrosswalkEntry(agency=AG, product="scheduling", native_id=f"B-{n}", entity="vehicle",
                                  canonical_id=f"veh-{n}", display_name=name))
    ok, failed = run(bus, svc, fault("f", "T-400", S, native_name="Bus 4"))
    reason = failed["f"].failurereason
    assert not ok and reason.startswith("ambiguous") and "veh-1" in reason and "veh-2" in reason
    sent = failed["f"].data["identity"]["candidates"]
    assert [c["canonical_id"] for c in sent] == ["veh-1", "veh-2"] and all(c["evidence"] for c in sent)
    [u] = xw.unresolved(AG)
    cands = u["resolution"]["candidates"]
    assert [c["canonical_id"] for c in cands] == ["veh-1", "veh-2"] and all(c["evidence"] for c in cands)


def test_f2_a_pick_never_rewrites_history(bus, svc, xw):
    """F2 repro: the old unit's event after the swap parks; the human picks veh-2. Events dated before the swap
    still belong to veh-1; events after the parked event's time belong to veh-2."""
    seed(xw)
    run(bus, svc, fault("late", "T-77", S + timedelta(hours=1)))
    assert svc.apply_mapping(CrosswalkEntry(agency=AG, product="telematics", native_id="T-77", entity="vehicle",
                                            canonical_id="veh-2")) == 1
    ok, _ = run(bus, svc, fault("old", "T-77", S - timedelta(days=2)), fault("new", "T-77", S + timedelta(hours=2)))
    assert ok["late"].canonicalid == "veh-2" and ok["new"].canonicalid == "veh-2"
    assert ok["old"].canonicalid == "veh-1"
    rows = sorted((r.valid_from or S - timedelta(days=99), r.canonical_id, r.valid_to)
                  for r in xw.rows(AG) if r.native_id == "T-77")
    assert rows == [(S - timedelta(days=99), "veh-1", S), (S + timedelta(hours=1), "veh-2", None)]


# ---------------------------------------------------------------- last fix round (R2 re-check)
def _t90_rows(xw):
    return sorted((r.valid_from, r.valid_to, r.canonical_id) for r in xw.rows(AG) if r.native_id == "T-90")


def test_g1_device_moved_releases_a_contradiction_parked_event(bus, svc, xw):
    """G1 (reviewer repro): T-90 -> veh-1; a fault carrying veh-2's VIN parks (F1). Picking the id's own vehicle
    re-parks it; 'device moved' to veh-2 closes veh-1's row at the event's time, opens veh-2's from it and
    republishes: the event resolves to veh-2; an event dated before still resolves to veh-1; a second identical
    move is a no-op."""
    seed(xw)
    at = S + timedelta(hours=1)
    run(bus, svc, fault("c", "T-90", at, native_vin=VIN_2))
    entry = lambda canon: CrosswalkEntry(agency=AG, product="telematics", native_id="T-90", entity="vehicle",  # noqa: E731
                                         canonical_id=canon)
    svc.apply_mapping(entry("veh-1"))                       # plain pick of the id's own vehicle
    ok, _ = run(bus, svc)
    assert "c" not in ok and [u["event_id"] for u in xw.unresolved(AG)] == ["c"]
    assert svc.apply_mapping(entry("veh-2"), move=True) == 1
    ok, _ = run(bus, svc, fault("before", "T-90", at - timedelta(minutes=30)))
    assert ok["c"].canonicalid == "veh-2" and ok["before"].canonicalid == "veh-1"
    assert _t90_rows(xw) == [(S, at, "veh-1"), (at, None, "veh-2")]
    assert svc.apply_mapping(entry("veh-2"), move=True) == 0  # identical move again: nothing changes
    assert _t90_rows(xw) == [(S, at, "veh-1"), (at, None, "veh-2")]


def test_g1_a_move_that_would_leave_overlapping_rows_is_refused(bus, svc, xw):
    from transitos.plumbing.crosswalk import CrosswalkConflict
    seed(xw)
    later = S + timedelta(hours=5)
    xw.resolve(CrosswalkEntry(agency=AG, product="telematics", native_id="T-90", entity="vehicle",
                              canonical_id="veh-3", valid_from=later))
    before = _t90_rows(xw)
    run(bus, svc, fault("c", "T-90", S + timedelta(hours=1), native_vin=VIN_2))
    try:
        svc.apply_mapping(CrosswalkEntry(agency=AG, product="telematics", native_id="T-90", entity="vehicle",
                                         canonical_id="veh-2"), move=True)
        raise AssertionError("move accepted")
    except CrosswalkConflict:
        pass
    assert _t90_rows(xw) == before and [u["event_id"] for u in xw.unresolved(AG)] == ["c"]


def test_g4_n5_a_pick_overlapping_another_vehicles_row_is_refused(bus, svc, xw):
    """n5: the overlap guard in pick_in (another vehicle's row covering the pick's window) must refuse."""
    from transitos.plumbing.crosswalk import CrosswalkConflict
    xw.resolve(CrosswalkEntry(agency=AG, product="telematics", native_id="T-5", entity="vehicle",
                              canonical_id="veh-1", valid_to=S + timedelta(days=1)))
    with xw.db.tx() as c:
        xw.add_unresolved_in(c, AG, "telematics", "T-5", fault("p", "T-5", S))
    try:
        xw.pick(CrosswalkEntry(agency=AG, product="telematics", native_id="T-5", entity="vehicle",
                               canonical_id="veh-2"))
        raise AssertionError("pick accepted")
    except CrosswalkConflict as e:
        assert e.conflict_row["canonical_id"] == "veh-1"
    assert [r.canonical_id for r in xw.rows(AG) if r.native_id == "T-5"] == ["veh-1"]


def test_g4_n7_an_ended_row_never_contradicts_the_fast_path(bus, svc, xw):
    """n7: veh-2's VIN sits on a row that ENDED before the event: no contradiction, the id's vehicle stands."""
    xw.resolve(CrosswalkEntry(agency=AG, product="scheduling", native_id="BUS-45", entity="vehicle",
                              canonical_id="veh-2", vin=VIN_2, valid_to=S))
    xw.resolve(CrosswalkEntry(agency=AG, product="telematics", native_id="T-90", entity="vehicle",
                              canonical_id="veh-1", valid_from=S))
    ok, failed = run(bus, svc, fault("e", "T-90", S + timedelta(hours=1), native_vin=VIN_2))
    assert ok["e"].canonicalid == "veh-1" and not failed
