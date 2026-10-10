import csv
import io

from transitos.mocks.clock import DEFAULT_START
from transitos.mocks.crosswalk_seed import all_crosswalk_entries, crosswalk_entries
from transitos.mocks.seed import AGENCIES, build_fleet

from .conftest import A


def test_fleet_shape():
    f = build_fleet("agency-042", DEFAULT_START)
    assert len(f.vehicles) == 12 and len(f.operators) == 10
    assert sum(v["role"] == "spare" for v in f.vehicles) == 3
    assert len(f.blocks) == 9 * 8
    assert any(b["needs"]["lift"] for b in f.blocks)
    assert f.vehicles[0]["sched"] == "BUS-44" and f.vehicles[0]["tele"] == "T-77" and f.vehicles[0]["maint"] == "1106"
    near = [p for p in f.pm if p["due_mi"] - p["meter_mi"] <= 100]
    assert len(near) == 1


def test_native_ids_unique_and_mismatched():
    for a in AGENCIES:
        f = build_fleet(a, DEFAULT_START)
        for key in ("sched", "tele", "maint"):
            ids = [v[key] for v in f.vehicles]
            assert len(set(ids)) == 12
        assert {v["sched"] for v in f.vehicles}.isdisjoint({v["tele"] for v in f.vehicles})


def test_every_vehicle_has_ids_in_three_products(make):
    xs = {(e.product, e.native_id): e.canonical_id for e in crosswalk_entries("agency-042")}
    sched, _ = make("scheduling")
    tele, _ = make("telematics")
    maint, _ = make("maintenance")
    sv = {v["id"] for v in sched.get("/vehicles", headers=A).json()}
    assert len(sv) == 12
    assets = {p["asset"] for p in maint.get("/pm-schedules", headers=A).json()}
    canon = {xs[("scheduling", s)] for s in sv}
    assert canon == {xs[("maintenance", m)] for m in assets} == {f"veh-{i}" for i in range(1, 13)}
    for v in build_fleet("agency-042", DEFAULT_START).vehicles:
        assert xs[("telematics", v["tele"])] == v["canonical"]
        assert tele.post("/sim/fault", json={"vehicle": v["tele"], "spn": 1, "fmi": 1}, headers=A).status_code == 200


def test_operators_in_workforce_match_crosswalk(make):
    wf, _ = make("workforce")
    rows = list(csv.DictReader(io.StringIO(wf.get("/export/operators.csv", headers=A).text)))
    xs = {e.native_id for e in crosswalk_entries("agency-042") if e.product == "workforce"}
    assert {r["operator"] for r in rows} == xs and len(rows) == 10


def test_second_agency_has_other_formats(make):
    sched, _ = make("scheduling", agency="agency-077")
    ids = [v["id"] for v in sched.get("/vehicles", headers={"X-Agency": "agency-077"}).json()]
    assert ids[0] == "V101"
    assert sched.get("/vehicles", headers=A).json() == []  # tenancy isolation
    assert len(all_crosswalk_entries()) == 2 * (12 * 3 + 10)


def test_agency_header_required(sched):
    c, _ = sched
    assert c.get("/vehicles").status_code == 400


def test_custom_fleet_can_be_registered_and_seeded():
    from fastapi.testclient import TestClient

    from transitos.mocks import scheduling
    from transitos.mocks.chaos import Chaos
    from transitos.mocks.clock import SimClock
    from transitos.mocks.seed import Fleet, known_agency, register_fleet

    def tiny(now):
        f = Fleet("agency-x")
        f.vehicles.append({"idx": 1, "canonical": "veh-1", "role": "spare", "sched": "X1", "tele": "TX1",
                           "maint": "M1", "name": "Bus X1", "type": "standard", "seats": 30,
                           "wheelchair_positions": 0, "lift_ok": False, "depot": "D", "status": "ready"})
        return f
    register_fleet("agency-x", tiny)
    try:
        c = TestClient(scheduling.create_app(SimClock(), Chaos(), None))
        assert c.post("/sim/seed", headers={"X-Agency": "agency-x"}).status_code == 200
        assert [v["id"] for v in c.get("/vehicles", headers={"X-Agency": "agency-x"}).json()] == ["X1"]
    finally:
        register_fleet("agency-x", None)
    assert not known_agency("agency-x")
