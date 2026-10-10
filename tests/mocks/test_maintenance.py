from .conftest import A


def test_asset_and_pm(maint):
    c, _ = maint
    assert c.get("/assets/1106", headers=A).json()["status"] == "active"
    assert c.get("/assets/9", headers=A).status_code == 404
    pm = {p["asset"]: p for p in c.get("/pm-schedules", headers=A).json()}
    assert len(pm) == 12
    near = [p for p in pm.values() if p["due_mi"] - p["meter_mi"] <= 100]
    assert [p["asset"] for p in near] == ["1108"]  # veh-3


def test_idempotent_create(maint):
    c, _ = maint
    body = {"asset": "1106", "description": "pull", "opened_by": "system"}
    a = c.post("/workorders", json=body, headers={**A, "Idempotency-Key": "k1"}).json()
    b = c.post("/workorders", json=body, headers={**A, "Idempotency-Key": "k1"}).json()
    d = c.post("/workorders", json=body, headers={**A, "Idempotency-Key": "k2"}).json()
    assert a == b and a["id"] != d["id"]
    assert len(c.get("/workorders", headers=A).json()) == 2
    # no key -> no dedupe
    c.post("/workorders", json=body, headers=A)
    c.post("/workorders", json=body, headers=A)
    assert len(c.get("/workorders", headers=A).json()) == 4


def test_idempotency_scoped_per_agency(make):
    c, _ = make("maintenance")
    c.post("/sim/seed", headers={"X-Agency": "agency-077"})
    h = {"Idempotency-Key": "k"}
    a = c.post("/workorders", json={"asset": "1106"}, headers={**A, **h}).json()
    b = c.post("/workorders", json={"asset": "A2001"}, headers={"X-Agency": "agency-077", **h}).json()
    assert a["asset"] == "1106" and b["asset"] == "A2001"


def test_create_validation(maint):
    c, _ = maint
    assert c.post("/workorders", json={"asset": "nope"}, headers=A).status_code == 404
    assert c.post("/workorders", json={"asset": "1106", "opened_by": "robot"}, headers=A).status_code == 422


def test_filters_and_get(maint, clock):
    c, _ = maint
    w1 = c.post("/workorders", json={"asset": "1106"}, headers=A).json()
    clock.advance(1)
    w2 = c.post("/workorders", json={"asset": "1107"}, headers=A).json()
    assert c.get(f"/workorders/{w1['id']}", headers=A).json() == w1
    assert [w["id"] for w in c.get("/workorders", params={"asset": "1107"}, headers=A).json()] == [w2["id"]]
    assert [w["id"] for w in c.get("/workorders", params={"updated_since": w2["updated_at"]}, headers=A).json()] == [w2["id"]]
    assert c.get("/workorders", params={"status": "closed"}, headers=A).json() == []
    assert c.get("/workorders/zzz", headers=A).status_code == 404


def test_cancel_only_system(maint):
    c, _ = maint
    h = c.post("/workorders", json={"asset": "1106", "opened_by": "human"}, headers=A).json()
    s = c.post("/workorders", json={"asset": "1106", "opened_by": "system"}, headers=A).json()
    assert c.post(f"/workorders/{h['id']}/cancel", headers=A).status_code == 409
    r = c.post(f"/workorders/{s['id']}/cancel", headers=A).json()
    assert r["status"] == "closed" and r["closed_at"]


def test_close_drives_state(maint, clock):
    c, _ = maint
    w = c.post("/workorders", json={"asset": "1106"}, headers=A).json()
    clock.advance(2)
    closed = c.post(f"/sim/workorders/{w['id']}/close", headers=A).json()
    assert closed["status"] == "closed" and closed["closed_at"] == clock.iso() and closed["updated_at"] > w["updated_at"]
    assert c.get("/workorders", params={"status": "closed"}, headers=A).json()[0]["id"] == w["id"]


def test_clock_auto_close(maint, clock):
    c, _ = maint
    c.post("/sim/config", json={"auto_close_hours": 4}, headers=A)
    w = c.post("/workorders", json={"asset": "1106"}, headers=A).json()
    clock.advance(3)
    assert c.get(f"/workorders/{w['id']}", headers=A).json()["status"] == "open"
    clock.advance(1.5)
    assert c.get(f"/workorders/{w['id']}", headers=A).json()["status"] == "closed"


def test_meter_drives_pm_overdue(maint, clock):
    c, _ = maint
    c.post("/sim/meter", json={"asset": "1108", "meter_mi": 25100}, headers=A)
    p = next(p for p in c.get("/pm-schedules", headers=A).json() if p["asset"] == "1108")
    assert p["meter_mi"] > p["due_mi"]
    assert c.get("/assets/1108", headers=A).json()["meter_mi"] == 25100


def test_clock_mileage_accrual_drives_pm_overdue(maint, clock):
    c, _ = maint
    c.post("/sim/config", json={"miles_per_hour": 20}, headers=A)
    before = next(p for p in c.get("/pm-schedules", headers=A).json() if p["asset"] == "1108")
    assert before["meter_mi"] < before["due_mi"]
    clock.advance(10)
    after = next(p for p in c.get("/pm-schedules", params={"updated_since": clock.iso()}, headers=A).json() if p["asset"] == "1108")
    assert after["meter_mi"] == before["meter_mi"] + 200 > after["due_mi"]


def test_chaos_429(maint):
    c, _ = maint
    c.post("/sim/chaos", json={"mode": "http_429", "on": True})
    r = c.post("/workorders", json={"asset": "1106"}, headers=A)
    assert r.status_code == 429 and r.headers["retry-after"]
    assert c.get("/pm-schedules", headers=A).status_code == 429
    c.post("/sim/chaos", json={"mode": "http_429", "on": False})
    assert c.post("/workorders", json={"asset": "1106"}, headers=A).status_code == 200


def test_429_does_not_create_wo(maint):
    c, chaos = maint
    chaos.set("http_429")
    c.post("/workorders", json={"asset": "1106"}, headers={**A, "Idempotency-Key": "z"})
    chaos.set("http_429", False)
    assert c.get("/workorders", headers=A).json() == []
