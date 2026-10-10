from .conftest import A


def test_vehicles_and_filters(sched, clock):
    c, _ = sched
    allv = c.get("/vehicles", headers=A).json()
    assert {"id", "seats", "wheelchair_positions", "lift_ok", "type", "status", "depot", "updated_at"} <= set(allv[0])
    assert len(c.get("/vehicles", params={"status": "ready"}, headers=A).json()) == 3
    assert c.get("/vehicles/BUS-44", headers=A).json()["status"] == "in_service"
    assert c.get("/vehicles/NOPE", headers=A).status_code == 404
    clock.advance(1)
    c.post("/vehicles/BUS-44/unavailable", json={"start": "x", "end": "y"}, headers=A)
    upd = c.get("/vehicles", params={"updated_since": clock.iso()}, headers=A).json()
    assert [v["id"] for v in upd] == ["BUS-44"]


def test_blocks_query(sched):
    c, _ = sched
    bl = c.get("/blocks", params={"vehicle": "BUS-44"}, headers=A).json()
    assert len(bl) == 8 and bl[0]["needs"]["lift"] is True
    win = c.get("/blocks", params={"vehicle": "BUS-44", "from": bl[2]["start"], "to": bl[2]["end"]}, headers=A).json()
    assert [b["id"] for b in win] == [bl[2]["id"]]


def test_unavailable_logs_status_change_no_idempotency(sched):
    c, _ = sched
    for _ in range(2):
        assert c.post("/vehicles/BUS-44/unavailable", json={}, headers=A).json()["status"] == "out_of_service"
    ch = c.get("/status-changes", headers=A).json()
    assert len(ch) == 2 and ch[0]["vehicle"] == "BUS-44"  # duplicates: no idempotency (A2 family)
    c.post("/vehicles/BUS-44/available", headers=A)
    assert c.get("/vehicles/BUS-44", headers=A).json()["status"] == "ready"


def test_no_block_cancel_endpoint(sched):
    c, _ = sched
    assert c.delete("/blocks/BLK-1-1", headers=A).status_code in (404, 405)
    assert c.post("/blocks/BLK-1-1/cancel", headers=A).status_code == 404


def test_reassign_to_suitable_spare(sched):
    c, _ = sched
    blk = c.get("/blocks", params={"vehicle": "BUS-44"}, headers=A).json()[0]  # needs lift
    r = c.post(f"/blocks/{blk['id']}/reassign", json={"vehicle": "BUS-53"}, headers=A)  # veh-10 spare, lift
    assert r.status_code == 200 and r.json()["vehicle"] == "BUS-53"
    # now taken for that window
    other = c.get("/blocks", params={"vehicle": "BUS-45"}, headers=A).json()[0]
    assert c.post(f"/blocks/{other['id']}/reassign", json={"vehicle": "BUS-53"}, headers=A).status_code == 409


def test_reassign_rejects_unsuitable_spare(sched):
    c, _ = sched
    blk = c.get("/blocks", params={"vehicle": "BUS-44"}, headers=A).json()[0]
    r = c.post(f"/blocks/{blk['id']}/reassign", json={"vehicle": "BUS-55"}, headers=A)  # veh-12 no lift
    assert r.status_code == 409 and ("lift" in r.json()["detail"] or "wheelchair" in r.json()["detail"])
    r = c.post(f"/blocks/{blk['id']}/reassign", json={"vehicle": "BUS-54"}, headers=A)  # veh-11 small cutaway
    assert r.status_code == 409


def test_reassign_rejects_out_of_service_target(sched):
    c, _ = sched
    c.post("/vehicles/BUS-53/unavailable", json={}, headers=A)
    blk = c.get("/blocks", params={"vehicle": "BUS-44"}, headers=A).json()[0]
    assert c.post(f"/blocks/{blk['id']}/reassign", json={"vehicle": "BUS-53"}, headers=A).status_code == 409


def test_chaos_reject_write(sched):
    c, _ = sched
    c.post("/sim/chaos", json={"mode": "reject_write", "on": True})
    blk = c.get("/blocks", params={"vehicle": "BUS-44"}, headers=A).json()[0]
    assert c.post(f"/blocks/{blk['id']}/reassign", json={"vehicle": "BUS-53"}, headers=A).status_code == 503
    assert c.post("/vehicles/BUS-44/unavailable", json={}, headers=A).status_code == 503
    assert c.get("/vehicles", headers=A).status_code == 200  # reads fine
    c.post("/sim/chaos", json={"mode": "reject_write", "on": False})
    assert c.post(f"/blocks/{blk['id']}/reassign", json={"vehicle": "BUS-53"}, headers=A).status_code == 200


def test_chaos_timeout(sched):
    c, chaos = sched
    chaos.set("timeout")
    assert c.get("/vehicles", headers=A).status_code == 504
    assert c.post("/sim/chaos", json={"mode": "timeout", "on": False}).status_code == 200
    assert c.get("/vehicles", headers=A).status_code == 200


def test_bad_chaos_mode(sched):
    c, _ = sched
    assert c.post("/sim/chaos", json={"mode": "bogus"}).status_code == 422


def test_reset(sched):
    c, _ = sched
    c.post("/sim/reset", headers=A)
    assert c.get("/vehicles", headers=A).json() == []
