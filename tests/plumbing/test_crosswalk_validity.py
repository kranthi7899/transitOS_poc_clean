"""Crosswalk v1.2 store (B-1): validity windows, identity evidence, candidates(), lookup(at=), current ids, parked
resolutions. Runs on SQLite here and on Postgres when a server is up (the `db` fixture), same SQL."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from transitos.contracts.crosswalk import Candidate, CrosswalkEntry, Resolution
from transitos.contracts.envelope import CloudEvent
from transitos.plumbing import Db, SqliteCrosswalk

AG = "ag1"
S = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def e(product, native, canon, **kw) -> CrosswalkEntry:
    return CrosswalkEntry(agency=AG, product=product, native_id=native, entity="vehicle", canonical_id=canon, **kw)


def ev(eid="e1", native="T-77") -> CloudEvent:
    return CloudEvent(id=eid, source="adapter/telematics/ag1", type="VehicleFaultDetected", time=S, agencyid=AG,
                      data={"native_vehicle_id": native, "spn": 1, "fmi": 1})


def swap(xw: SqliteCrosswalk) -> None:
    xw.resolve(e("telematics", "T-77", "veh-1", valid_to=S))
    xw.resolve(e("telematics", "T-90", "veh-1", valid_from=S))
    xw.resolve(e("telematics", "T-77", "veh-5", valid_from=S))       # the old unit moved to another bus


def test_lookup_at_respects_valid_from_inclusive_and_valid_to_exclusive(db):
    xw = SqliteCrosswalk(db)
    swap(xw)
    assert xw.lookup(AG, "telematics", "T-77", at=S - timedelta(microseconds=1)) == "veh-1"
    assert xw.lookup(AG, "telematics", "T-77", at=S) == "veh-5"
    assert xw.lookup(AG, "telematics", "T-90", at=S) == "veh-1"
    assert xw.lookup(AG, "telematics", "T-90", at=S - timedelta(microseconds=1)) is None


def test_lookup_without_at_is_the_current_open_ended_row(db):
    xw = SqliteCrosswalk(db)
    swap(xw)
    assert xw.lookup(AG, "telematics", "T-77") == "veh-5"
    xw.resolve(e("maintenance", "1106", "veh-1", valid_to=S))
    assert xw.lookup(AG, "maintenance", "1106") is None               # ended, nothing current


def test_candidates_returns_the_rows_valid_at_that_time(db):
    xw = SqliteCrosswalk(db)
    swap(xw)
    assert [c.canonical_id for c in xw.candidates(AG, "telematics", "T-77", at=S - timedelta(seconds=1))] == ["veh-1"]
    assert [c.canonical_id for c in xw.candidates(AG, "telematics", "T-77", at=S)] == ["veh-5"]
    assert sorted(c.canonical_id for c in xw.candidates(AG, "telematics", "T-77")) == ["veh-5"]


def test_all_ids_prefers_the_current_number_after_a_renumbering(db):
    xw = SqliteCrosswalk(db)
    xw.resolve(e("maintenance", "1106", "veh-1", valid_to=S))
    xw.resolve(e("maintenance", "2206", "veh-1", valid_from=S))
    xw.resolve(e("scheduling", "BUS-44", "veh-1"))
    assert xw.all_ids(AG, "veh-1") == {"maintenance": "2206", "scheduling": "BUS-44"}


def test_evidence_columns_round_trip_and_rows_lists_the_namespace(db):
    xw = SqliteCrosswalk(db)
    xw.resolve(e("scheduling", "BUS-44", "veh-1", vin="0ZBB1C2D0RZ000001", plate="ZZ 4KTR219",
                 display_name="Bus 44", valid_from=S))
    xw.resolve(CrosswalkEntry(agency="other", product="scheduling", native_id="X", entity="vehicle", canonical_id="v"))
    rows = xw.rows(AG)
    assert len(rows) == 1
    r = rows[0]
    assert (r.vin, r.plate, r.display_name, r.valid_from, r.valid_to) == \
        ("0ZBB1C2D0RZ000001", "ZZ 4KTR219", "Bus 44", S, None)


def test_naive_validity_bounds_are_stored_as_utc(db):
    xw = SqliteCrosswalk(db)
    xw.resolve(e("telematics", "T-1", "veh-1", valid_from=S.replace(tzinfo=None)))
    assert xw.rows(AG)[0].valid_from == S
    assert xw.lookup(AG, "telematics", "T-1", at=S) == "veh-1"


def test_parked_event_keeps_its_resolution_for_the_panel(db):
    xw = SqliteCrosswalk(db)
    res = Resolution(outcome="AMBIGUOUS", confidence=0.73, candidates=[
        Candidate(canonical_id="veh-1", score=0.73, evidence=["fleet number BUS4 ~ BUS44"]),
        Candidate(canonical_id="veh-2", score=0.73, evidence=["fleet number BUS4 ~ BUS45"])])
    with db.tx() as c:
        xw.add_unresolved_in(c, AG, "telematics", "T-400", ev("p1", "T-400"), resolution=res, notes=["n1"])
    [u] = xw.unresolved(AG)
    assert u["resolution"]["outcome"] == "AMBIGUOUS"
    assert [c["canonical_id"] for c in u["resolution"]["candidates"]] == ["veh-1", "veh-2"]
    assert u["resolution"]["notes"] == ["n1"]
    assert [x.id for x in xw.resolve(e("telematics", "T-400", "veh-1"))] == ["p1"]


def test_a_v1_table_is_migrated_in_place(tmp_path):
    db = Db(str(tmp_path / "old.db"))
    db.script("CREATE TABLE xw_entries(agency TEXT NOT NULL, product TEXT NOT NULL, native_id TEXT NOT NULL, "
              "entity TEXT NOT NULL, canonical_id TEXT NOT NULL, PRIMARY KEY(agency, product, native_id));"
              "CREATE TABLE xw_unresolved(agency TEXT NOT NULL, product TEXT NOT NULL, native_id TEXT NOT NULL, "
              "event_id TEXT NOT NULL, payload TEXT, PRIMARY KEY(agency, product, native_id, event_id));")
    db.execute("INSERT INTO xw_entries VALUES('ag1','telematics','T-1','vehicle','veh-1')")
    xw = SqliteCrosswalk(db)
    assert xw.lookup(AG, "telematics", "T-1") == "veh-1"
    xw.resolve(e("telematics", "T-1", "veh-2", valid_from=S))
    assert xw.lookup(AG, "telematics", "T-1", at=S) == "veh-2"
    xw.add_unresolved(AG, "telematics", "T-2", "x")
    assert xw.unresolved(AG)[0]["resolution"] is None
