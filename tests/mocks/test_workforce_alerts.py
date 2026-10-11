import csv
import io

from .conftest import A


def test_csv_export(make):
    c, _ = make("workforce")
    r = c.get("/export/operators.csv", headers=A)
    assert r.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert list(rows[0]) == ["operator", "qualifications", "lift_trained", "hours_today", "hours_week",
                             "available_from", "available_to", "on_extra_board",
                             "licence_class", "endorsements", "trained_vehicle_types", "training_valid_to",
                             "medical_valid_to", "last_duty_end", "duty_start_today"]
    assert rows[0]["operator"] == "OP-310" and ";" in rows[0]["qualifications"]
    assert any(float(r["hours_today"]) >= 9 for r in rows) and any(r["on_extra_board"] == "true" for r in rows)
    assert any(r["lift_trained"] == "false" for r in rows)


def test_operator_and_assign(make):
    c, _ = make("workforce")
    assert c.get("/operators/OP-310", headers=A).json()["operator"] == "OP-310"
    assert c.get("/operators/zz", headers=A).status_code == 404
    r = c.post("/blocks/BLK-1-1/operator", json={"operator": "OP-319"}, headers=A).json()
    assert r["operator"] == "OP-319" and r["previous"] == "OP-310"
    assert c.get("/assignments/BLK-1-1", headers=A).json()["operator"] == "OP-319"
    assert c.post("/blocks/NOPE/operator", json={"operator": "OP-319"}, headers=A).status_code == 404
    assert c.post("/blocks/BLK-1-1/operator", json={"operator": "bad"}, headers=A).status_code == 404


def test_assign_does_not_enforce_hours(make):
    c, _ = make("workforce")
    # OP-312 has 9.5h today; the mock accepts anyway (limits are tool code)
    assert c.post("/blocks/BLK-1-1/operator", json={"operator": "OP-312"}, headers=A).status_code == 200


def test_chaos_reject_write(make):
    c, chaos = make("workforce")
    chaos.set("reject_write")
    assert c.post("/blocks/BLK-1-1/operator", json={"operator": "OP-319"}, headers=A).status_code == 503
    assert c.get("/assignments/BLK-1-1", headers=A).json()["operator"] == "OP-310"
    assert c.get("/export/operators.csv", headers=A).status_code == 200


def test_availability_changes(make, clock):
    c, _ = make("workforce")
    clock.advance(1)
    c.post("/sim/operator", json={"operator": "OP-311", "status": "unavailable"}, headers=A)
    ch = c.get("/availability-changes", headers=A).json()
    assert ch[0]["operator"] == "OP-311" and ch[0]["status"] == "unavailable"
    clock.advance(1)
    assert c.get("/availability-changes", params={"since": clock.iso()}, headers=A).json() == []


def test_alert_eventually_visible(make, clock):
    c, _ = make("alerts")
    aid = c.post("/alerts", json={"block": "BLK-1-1", "header_text": "h", "description_text": "d"}, headers=A).json()["id"]
    assert c.get("/alerts", headers=A).json() == []
    clock.advance(minutes=4)
    assert c.get("/alerts", headers=A).json() == []
    clock.advance(minutes=2)
    got = c.get("/alerts", headers=A).json()
    assert [a["id"] for a in got] == [aid] and got[0]["header_text"] == "h"


def test_alert_late_chaos(make, clock):
    c, chaos = make("alerts")
    chaos.set("late")
    c.post("/alerts", json={"block": "B", "header_text": "h"}, headers=A)
    clock.advance(minutes=10)
    assert c.get("/alerts", headers=A).json() == []
    clock.advance(minutes=25)
    assert len(c.get("/alerts", headers=A).json()) == 1


def test_alert_correction_supersedes_and_no_idempotency(make, clock):
    c, _ = make("alerts")
    a = c.post("/alerts", json={"block": "B", "header_text": "old"}, headers=A).json()["id"]
    c.post("/alerts", json={"block": "B", "header_text": "old"}, headers=A)  # duplicates allowed
    clock.advance(minutes=6)
    assert len(c.get("/alerts", headers=A).json()) == 2
    cid = c.post(f"/alerts/{a}/correction", json={"header_text": "new"}, headers=A).json()["id"]
    assert {x["id"] for x in c.get("/alerts", headers=A).json()} != {a, cid}  # correction not yet visible
    assert any(x["id"] == a for x in c.get("/alerts", headers=A).json())
    clock.advance(minutes=6)
    ids = {x["id"]: x for x in c.get("/alerts", headers=A).json()}
    assert a not in ids and ids[cid]["header_text"] == "new" and ids[cid]["corrects"] == a
    assert c.post("/alerts/zz/correction", json={}, headers=A).status_code == 404


def test_alert_chaos_timeout(make):
    c, chaos = make("alerts")
    chaos.set("timeout")
    assert c.get("/alerts", headers=A).status_code == 504


def test_run_build_all():
    from transitos.mocks.run import PORTS, build_all
    apps = build_all()
    assert set(apps) == set(PORTS)


def test_csv_carries_licence_endorsement_and_dated_training(make):
    c, _ = make("workforce")
    rows = {r["operator"]: r for r in csv.DictReader(io.StringIO(c.get("/export/operators.csv", headers=A).text))}
    artic, plain = rows["OP-310"], rows["OP-311"]            # OP-310 holds ARTIC in the legacy string
    assert (artic["licence_class"], plain["licence_class"]) == ("A", "B")
    assert artic["endorsements"] == "P" and "articulated" in artic["trained_vehicle_types"].split(";")
    assert "articulated" not in plain["trained_vehicle_types"].split(";") and "standard" in plain["trained_vehicle_types"]
    assert all(k in artic["training_valid_to"] for k in ("P=", "standard=", "lift="))
    assert "lift=" not in rows["OP-313"]["training_valid_to"]    # not lift trained -> no lift credential
    assert artic["last_duty_end"] == "" and artic["duty_start_today"] == ""   # optional rules: no data


def test_seed_is_deterministic(make):
    a, _ = make("workforce")
    first = a.get("/export/operators.csv", headers=A).text
    a.post("/sim/reset", headers=A)
    a.post("/sim/seed", headers=A)
    assert a.get("/export/operators.csv", headers=A).text == first


def test_scenario_switch_drops_operator_columns_and_reset_restores(make):
    c, _ = make("workforce")
    r = c.post("/sim/operator-fields", json={"drop": ["licence_class", "hours_week", "operator", "nonsense"]}, headers=A)
    assert r.json() == {"drop": ["licence_class", "hours_week"]}
    header = next(csv.reader(io.StringIO(c.get("/export/operators.csv", headers=A).text)))
    assert "licence_class" not in header and "hours_week" not in header and "operator" in header
    c.post("/sim/reset", headers=A)
    c.post("/sim/seed", headers=A)
    assert "licence_class" in next(csv.reader(io.StringIO(c.get("/export/operators.csv", headers=A).text)))
