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


V1 = ("CREATE TABLE xw_entries(agency TEXT NOT NULL, product TEXT NOT NULL, native_id TEXT NOT NULL, "
      "entity TEXT NOT NULL, canonical_id TEXT NOT NULL, PRIMARY KEY(agency, product, native_id));")


def _v1(tmp_path, name="old.db") -> Db:
    db = Db(str(tmp_path / name))
    db.script(V1)
    db.execute("INSERT INTO xw_entries VALUES('ag1','telematics','T-1','vehicle','veh-1')")
    db.execute("INSERT INTO xw_entries VALUES('ag1','maintenance','M-1','asset','veh-1')")
    return db


def test_f7_a_crash_inside_the_migration_loses_nothing(tmp_path, monkeypatch):
    """F7: the migration is one transaction; a crash between DROP TABLE and the RENAME rolls everything back."""
    db = _v1(tmp_path)

    def crash(stmt: str) -> None:
        if stmt.upper().startswith("ALTER TABLE XW_ENTRIES_V2 RENAME"):
            raise KeyboardInterrupt("process killed between DROP and RENAME")

    monkeypatch.setattr(SqliteCrosswalk, "_before_migration_statement", staticmethod(crash))
    try:
        SqliteCrosswalk(db)
    except KeyboardInterrupt:
        pass
    monkeypatch.undo()
    db.close()
    xw = SqliteCrosswalk(Db(str(tmp_path / "old.db")))          # next start
    assert xw.lookup(AG, "telematics", "T-1") == "veh-1" and xw.all_ids(AG, "veh-1") == \
        {"telematics": "T-1", "maintenance": "M-1"}


def test_f7_start_up_recovers_an_orphaned_v2_table(tmp_path):
    """A store left by the old non-atomic migration after DROP but before RENAME: only xw_entries_v2 exists."""
    db = Db(str(tmp_path / "orphan.db"))
    from transitos.plumbing.crosswalk import _ENTRIES
    db.script(f"CREATE TABLE xw_entries_v2{_ENTRIES};")
    db.execute("INSERT INTO xw_entries_v2(agency,product,native_id,vf,entity,canonical_id) "
               "VALUES('ag1','telematics','T-1','','vehicle','veh-1')")
    xw = SqliteCrosswalk(db)
    assert xw.lookup(AG, "telematics", "T-1") == "veh-1"
    assert not xw._has("xw_entries_v2", "vf")


def test_f7_start_up_discards_a_half_copied_v2_when_the_old_table_survived(tmp_path):
    """Crash before DROP: the old table is intact, a partial v2 exists; the migration is redone from the old table."""
    db = _v1(tmp_path)
    from transitos.plumbing.crosswalk import _ENTRIES
    db.script(f"CREATE TABLE xw_entries_v2{_ENTRIES};")   # empty: the copy never finished
    xw = SqliteCrosswalk(db)
    assert xw.all_ids(AG, "veh-1") == {"telematics": "T-1", "maintenance": "M-1"}
    assert not xw._has("xw_entries_v2", "vf")


# ---- bug sweep A, G3 (A147-A148): all_ids honours validity and entity -----------------------------------------
# Written from the ruling before the code: all_ids filters by validity at `at` (valid_from inclusive, valid_to
# exclusive) and by entity (a vehicle's vehicle/asset rows only); `at` None = now on the store's clock when it has
# one; no valid row -> the product is MISSING from the result (unknown), never a stale row.

def test_g3_an_ended_only_row_is_missing_not_current(db):
    xw = SqliteCrosswalk(db)
    xw.resolve(e("maintenance", "1106", "veh-1", valid_to=S))
    xw.resolve(e("scheduling", "BUS-44", "veh-1"))
    assert xw.all_ids(AG, "veh-1", at=S + timedelta(hours=1)) == {"scheduling": "BUS-44"}
    assert xw.all_ids(AG, "veh-1", at=S) == {"scheduling": "BUS-44"}                      # valid_to exclusive
    assert xw.all_ids(AG, "veh-1", at=S - timedelta(microseconds=1))["maintenance"] == "1106"


def test_g3_a_future_dated_open_row_does_not_win_over_the_current_row(db):
    xw = SqliteCrosswalk(db)
    xw.resolve(e("maintenance", "1106", "veh-1", valid_to=S + timedelta(days=1)))          # current until tomorrow
    xw.resolve(e("maintenance", "M-1", "veh-1", valid_from=S + timedelta(days=1)))         # open, starts tomorrow
    assert xw.all_ids(AG, "veh-1", at=S) == {"maintenance": "1106"}
    assert xw.all_ids(AG, "veh-1", at=S + timedelta(days=1)) == {"maintenance": "M-1"}     # valid_from inclusive


def test_g3_a_future_dated_row_alone_is_missing(db):
    xw = SqliteCrosswalk(db)
    xw.resolve(e("telematics", "T-9", "veh-1", valid_from=S + timedelta(hours=2)))
    assert xw.all_ids(AG, "veh-1", at=S) == {}


def test_g3_an_operator_row_is_never_returned_for_a_vehicle_product(db):
    xw = SqliteCrosswalk(db)
    xw.resolve(CrosswalkEntry(agency=AG, product="maintenance", native_id="OP-7", entity="operator",
                              canonical_id="veh-1"))
    assert "maintenance" not in xw.all_ids(AG, "veh-1", at=S)
    xw.resolve(CrosswalkEntry(agency=AG, product="maintenance", native_id="1106", entity="asset",
                              canonical_id="veh-1", valid_from=S - timedelta(days=1)))
    assert xw.all_ids(AG, "veh-1", at=S) == {"maintenance": "1106"}    # the asset row, the operator row ignored


def test_g3_operator_ids_are_asked_for_explicitly(db):
    xw = SqliteCrosswalk(db)
    xw.resolve(CrosswalkEntry(agency=AG, product="workforce", native_id="OP-1", entity="operator",
                              canonical_id="op-1"))
    assert xw.all_ids(AG, "op-1", at=S) == {}                                   # default: vehicle/asset rows only
    assert xw.all_ids(AG, "op-1", at=S, entities=("operator",)) == {"workforce": "OP-1"}


def test_g3_without_at_the_stores_clock_is_now(db):
    now = [S]
    xw = SqliteCrosswalk(db, clock=lambda: now[0])
    xw.resolve(e("maintenance", "1106", "veh-1", valid_to=S + timedelta(hours=1)))
    xw.resolve(e("maintenance", "M-1", "veh-1", valid_from=S + timedelta(hours=1)))
    assert xw.all_ids(AG, "veh-1") == {"maintenance": "1106"}
    now[0] = S + timedelta(hours=2)
    assert xw.all_ids(AG, "veh-1") == {"maintenance": "M-1"}


def test_g3_reused_asset_id_ended_vehicle_is_missing_the_new_owner_reads_its_row(db):
    """Asset 1107 belonged to veh-2 until S, then was given to veh-3 (veh-3's own 1108 ended at S)."""
    xw = SqliteCrosswalk(db, clock=lambda: S + timedelta(hours=1))
    xw.resolve(e("maintenance", "1107", "veh-2", valid_to=S))
    xw.resolve(e("maintenance", "1107", "veh-3", valid_from=S))
    xw.resolve(e("maintenance", "1108", "veh-3", valid_to=S))
    assert "maintenance" not in xw.all_ids(AG, "veh-2")
    assert xw.all_ids(AG, "veh-3") == {"maintenance": "1107"}
