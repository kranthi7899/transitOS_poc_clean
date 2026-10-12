"""Phase 10 Step 4 (Track A): pending repairs, the shop status vocabulary, and the local-time knob (default OFF)."""
from __future__ import annotations

import csv
import io
import re
from datetime import UTC, datetime

import pytest

from transitos.mocks.clock import SimClock

from .conftest import A

NAIVE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")
LOCAL = {"local_tz": "America/New_York", "utc_offset": True, "epoch_ms": True}


@pytest.fixture
def fallback_clock():
    return SimClock(datetime(2026, 11, 1, 5, 30, tzinfo=UTC))   # 01:30 EDT, the FIRST 01:30 of the fall-back day


# ------------------------------------------------------------------ pending repairs (4.1)
def pr_post(c, key="k1", asset="1106", **kw):
    return c.post("/pending-repairs", json={"asset": asset, "description": "coolant", "requested_by": "system", **kw},
                  headers={**A, "Idempotency-Key": key})


def test_pending_repair_is_created_open_and_marked_simulated(maint):
    c, _ = maint
    r = pr_post(c)
    assert r.status_code == 200
    pr = r.json()
    assert pr["status"] == "OPEN" and pr["work_order"] is None and pr["asset"] == "1106"
    assert pr["simulated"] is True
    listed = c.get("/pending-repairs", headers=A).json()
    assert [p["id"] for p in listed] == [pr["id"]] and all(p["simulated"] is True for p in listed)
    assert c.get(f"/pending-repairs/{pr['id']}", headers=A).json()["id"] == pr["id"]
    assert c.get("/workorders", headers=A).json() == []          # a pending repair is not a work order


def test_pending_repair_is_idempotent_by_key_and_filterable(maint):
    c, _ = maint
    a, b = pr_post(c, "k1").json(), pr_post(c, "k1").json()
    other = pr_post(c, "k2").json()
    assert a["id"] == b["id"] != other["id"]
    assert [p["id"] for p in c.get("/pending-repairs", params={"idempotency_key": "k1"}, headers=A).json()] == [a["id"]]
    assert len(c.get("/pending-repairs", params={"asset": "1106", "status": "OPEN"}, headers=A).json()) == 2


def test_pending_repair_validation(maint):
    c, _ = maint
    assert pr_post(c, asset="nope").status_code == 404
    assert pr_post(c, requested_by="robot").status_code == 422
    assert c.get("/pending-repairs/pr-404", headers=A).status_code == 404


def test_foreman_conversion_creates_a_shop_vocabulary_work_order_from_the_request(maint):
    c, _ = maint
    pr = pr_post(c).json()
    out = c.post(f"/sim/pending-repairs/{pr['id']}/convert", headers=A).json()
    wo = out["work_order"]
    assert wo["status"] == "OPEN" and wo["opened_by"] == "system" and wo["from_pending_repair"] == pr["id"]
    back = c.get(f"/pending-repairs/{pr['id']}", headers=A).json()
    assert back["status"] == "CONVERTED" and back["work_order"] == wo["id"]
    # the same key now reads back the converted request (no second pending repair, no second WO)
    again = pr_post(c).json()
    assert again["id"] == pr["id"] and again["work_order"] == wo["id"]
    assert len(c.get("/workorders", headers=A).json()) == 1
    # converting twice is a no-op
    assert c.post(f"/sim/pending-repairs/{pr['id']}/convert", headers=A).json()["work_order"]["id"] == wo["id"]


def test_conversion_attaches_to_a_work_order_a_person_opened_first(maint):
    c, _ = maint
    pr = pr_post(c).json()
    human = c.post("/workorders", json={"asset": "1106", "description": "brakes", "opened_by": "human"},
                   headers=A).json()
    out = c.post(f"/sim/pending-repairs/{pr['id']}/convert", headers=A).json()
    assert out["work_order"]["id"] == human["id"]
    assert [w["id"] for w in c.get("/workorders", headers=A).json()] == [human["id"]]   # no duplicate
    assert c.get(f"/pending-repairs/{pr['id']}", headers=A).json()["work_order"] == human["id"]


@pytest.mark.parametrize("status", ["IN_PROGRESS", "WAITING_PARTS", "PENDING", "COMPLETE"])
def test_shop_statuses_are_settable_and_none_of_them_closes(maint, status):
    c, _ = maint
    pr = pr_post(c).json()
    wo = c.post(f"/sim/pending-repairs/{pr['id']}/convert", headers=A).json()["work_order"]
    w = c.post(f"/sim/workorders/{wo['id']}/status", json={"status": status}, headers=A).json()
    assert w["status"] == status and w["closed_at"] is None
    assert c.post(f"/sim/workorders/{wo['id']}/status", json={"status": "DONE"}, headers=A).status_code == 422


def test_closing_a_shop_work_order_uses_closed(maint):
    c, _ = maint
    pr = pr_post(c).json()
    wo = c.post(f"/sim/pending-repairs/{pr['id']}/convert", headers=A).json()["work_order"]
    w = c.post(f"/sim/workorders/{wo['id']}/close", headers=A).json()
    assert w["status"] == "CLOSED" and w["closed_at"]


def test_cancel_open_pending_repair_and_cancel_rules(maint):
    c, _ = maint
    pr = pr_post(c).json()
    assert c.post(f"/pending-repairs/{pr['id']}/cancel", headers=A).json()["status"] == "CANCELLED"
    assert c.post(f"/pending-repairs/{pr['id']}/cancel", headers=A).json()["status"] == "CANCELLED"   # idempotent
    c.post(f"/sim/pending-repairs/{pr['id']}/convert", headers=A)       # never converts a cancelled request
    assert c.get("/workorders", headers=A).json() == []
    # a request that CREATED its work order: cancel the work order, not the request
    pr2 = pr_post(c, "k2").json()
    c.post(f"/sim/pending-repairs/{pr2['id']}/convert", headers=A)
    assert c.post(f"/pending-repairs/{pr2['id']}/cancel", headers=A).status_code == 409
    # a request attached to a person's work order: withdrawn, the person's work order untouched
    human = c.post("/workorders", json={"asset": "1107", "opened_by": "human"}, headers=A).json()
    pr3 = pr_post(c, "k3", asset="1107").json()
    c.post(f"/sim/pending-repairs/{pr3['id']}/convert", headers=A)
    assert c.post(f"/pending-repairs/{pr3['id']}/cancel", headers=A).json()["status"] == "CANCELLED"
    assert c.get(f"/workorders/{human['id']}", headers=A).json()["status"] == "open"


def test_retired_asset_takes_no_pending_repair(make):
    c, _ = make("maintenance", agency="agency-088")
    h = {"X-Agency": "agency-088"}
    retired = [a for a in c.get("/assets", headers=h).json() if a["status"] == "retired"]
    assert retired
    r = c.post("/pending-repairs", json={"asset": retired[0]["asset"]}, headers={**h, "Idempotency-Key": "r"})
    assert r.status_code == 409


# ------------------------------------------------------------------ local-time knob (4.2), default OFF
def test_default_off_maintenance_keeps_utc_and_no_offset_field(maint):
    c, _ = maint
    wo = c.post("/workorders", json={"asset": "1106"}, headers=A).json()
    assert wo["opened_at"].endswith("Z") and "utc_offset" not in wo
    assert all(p["updated_at"].endswith("Z") for p in c.get("/pm-schedules", headers=A).json())
    assert wo["status"] == "open"                    # legacy vocabulary unless the product is switched to shop


def test_default_off_workforce_csv_and_telematics_unchanged(make):
    wf, _ = make("workforce")
    rows = list(csv.DictReader(io.StringIO(wf.get("/export/operators.csv", headers=A).text)))
    assert rows[0]["available_from"].endswith("Z")
    tel, _ = make("telematics", sender=lambda *a: None)
    p = tel.post("/sim/fault", json={"vehicle": "T-77", "spn": 110, "fmi": 0}, headers=A).json()
    assert isinstance(p["time"], str) and p["time"].endswith("Z")


def test_local_time_knob_renders_zone_less_local_time_with_offsets(make, fallback_clock):
    from transitos.mocks import maintenance
    from transitos.mocks.chaos import Chaos
    from fastapi.testclient import TestClient
    c = TestClient(maintenance.create_app(fallback_clock, Chaos(), None))
    c.post("/sim/seed", headers=A)
    assert c.post("/sim/time-format", json=LOCAL, headers=A).status_code == 200
    first = c.post("/workorders", json={"asset": "1106"}, headers=A).json()          # 05:30Z = 01:30 EDT
    fallback_clock.advance(hours=1)                                                   # 06:30Z = 01:30 EST
    second = c.post("/workorders", json={"asset": "1107"}, headers=A).json()
    assert first["opened_at"] == second["opened_at"] == "2026-11-01T01:30:00"         # the same wall time twice
    assert NAIVE.match(first["opened_at"])
    assert first["utc_offset"]["opened_at"] == "-04:00" and second["utc_offset"]["opened_at"] == "-05:00"
    # the store keeps UTC: the knob is render-only
    st = c.app.state.ctx.store
    assert st.get("agency-042", "wo", first["id"])["opened_at"] == "2026-11-01T05:30:00Z"
    # updated_since in local time: the ambiguous hour is read as its FIRST occurrence (never misses an update)
    got = c.get("/workorders", params={"updated_since": "2026-11-01T01:30:00"}, headers=A).json()
    assert {w["id"] for w in got} == {first["id"], second["id"]}
    pm = c.get("/pm-schedules", headers=A).json()
    assert all(NAIVE.match(p["updated_at"]) for p in pm)


def test_local_time_knob_without_offsets(make):
    c, _ = make("maintenance")
    c.post("/sim/time-format", json={"local_tz": "America/New_York"}, headers=A)
    wo = c.post("/workorders", json={"asset": "1106"}, headers=A).json()
    assert NAIVE.match(wo["opened_at"]) and "utc_offset" not in wo


def test_workforce_csv_local_time(make, clock):
    wf, _ = make("workforce")
    wf.post("/sim/time-format", json=LOCAL, headers=A)
    rows = list(csv.DictReader(io.StringIO(wf.get("/export/operators.csv", headers=A).text)))
    for col in ("available_from", "available_to", "medical_valid_to"):
        vals = [r[col] for r in rows if r[col]]
        assert vals and all(NAIVE.match(v) for v in vals), col
    dated = [p.partition("=")[2] for r in rows for p in r["training_valid_to"].split(";") if "=" in p]
    assert dated and all(NAIVE.match(v) for v in dated)
    # the JSON API keeps UTC (only the daily CSV export is local, A108)
    assert wf.get("/operators/OP-310", headers=A).json()["available_from"].endswith("Z")


def test_telematics_epoch_milliseconds(make, clock):
    tel, _ = make("telematics", sender=lambda *a: None)
    tel.post("/sim/time-format", json=LOCAL, headers=A)
    p = tel.post("/sim/fault", json={"vehicle": "T-77", "spn": 110, "fmi": 0}, headers=A).json()
    assert p["time"] == int(clock.now().timestamp() * 1000)
    feed = tel.get("/faults/feed", headers=A).json()["items"]
    assert feed and all(isinstance(i["time"], int) for i in feed)


def test_time_format_validation(make):
    c, _ = make("maintenance")
    assert c.post("/sim/time-format", json={"local_tz": "Mars/Olympus"}, headers=A).status_code == 422
    assert c.post("/sim/time-format", json={}, headers=A).json() == {"local_tz": None, "utc_offset": False,
                                                                     "epoch_ms": False}
