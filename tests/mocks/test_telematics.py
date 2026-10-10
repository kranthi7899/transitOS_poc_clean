import json

from transitos.mocks.telematics import sign, verify

from .conftest import A


class Sink:
    def __init__(self):
        self.got = []

    def __call__(self, url, body, headers):
        self.got.append((url, body, headers))


def setup(make):
    sink = Sink()
    c, chaos = make("telematics", sender=sink)
    c.post("/sim/webhook", json={"url": "http://hook/x"}, headers=A)
    return c, chaos, sink


def test_fault_webhook_signed(make):
    c, _, sink = setup(make)
    p = c.post("/sim/fault", json={"vehicle": "T-77", "spn": 100, "fmi": 1, "lamps": {"stop": True}}, headers=A).json()
    assert len(sink.got) == 1
    url, body, h = sink.got[0]
    assert url == "http://hook/x" and verify(body, h["X-Signature"]) and h["X-Signature"].startswith("sha256=")
    assert not verify(body + b" ", h["X-Signature"])
    d = json.loads(body)
    assert d == p and d["vehicle"]["id"] == "T-77" and d["fault"]["lamps"]["stop"] is True
    assert set(d) == {"eventId", "vehicle", "fault", "time"} and d["fault"]["occurrenceCount"] == 1
    assert sign(b"x") == sign(b"x")


def test_occurrence_count_and_current(make):
    c, _, _ = setup(make)
    for _ in range(3):
        c.post("/sim/fault", json={"vehicle": "T-77", "spn": 100, "fmi": 1}, headers=A)
    cur = c.get("/faults/current", params={"vehicle": "T-77"}, headers=A).json()
    assert len(cur) == 1 and cur[0]["fault"]["occurrenceCount"] == 3
    assert c.get("/faults/current", params={"vehicle": "T-78"}, headers=A).json() == []
    c.post("/sim/fault/clear", json={"vehicle": "T-77", "spn": 100, "fmi": 1}, headers=A)
    assert c.get("/faults/current", headers=A).json() == []


def test_feed_cursor(make):
    c, _, _ = setup(make)
    c.post("/sim/fault", json={"vehicle": "T-77", "spn": 1, "fmi": 1}, headers=A)
    f = c.get("/faults/feed", headers=A).json()
    assert len(f["items"]) == 1
    c.post("/sim/fault", json={"vehicle": "T-78", "spn": 2, "fmi": 1}, headers=A)
    f2 = c.get("/faults/feed", params={"cursor": f["next_cursor"]}, headers=A).json()
    assert [i["vehicle"]["id"] for i in f2["items"]] == ["T-78"]
    f3 = c.get("/faults/feed", params={"cursor": f2["next_cursor"]}, headers=A).json()
    assert f3["items"] == [] and f3["next_cursor"] == f2["next_cursor"]


def test_chaos_duplicate(make):
    c, chaos, sink = setup(make)
    chaos.set("duplicate")
    p = c.post("/sim/fault", json={"vehicle": "T-77", "spn": 1, "fmi": 1}, headers=A).json()
    assert len(sink.got) == 2 and sink.got[0][1] == sink.got[1][1]
    assert json.loads(sink.got[0][1])["eventId"] == p["eventId"]
    assert len(c.get("/faults/feed", headers=A).json()["items"]) == 2


def test_chaos_late_and_out_of_order(make, clock):
    c, chaos, sink = setup(make)
    chaos.set("late")
    late = c.post("/sim/fault", json={"vehicle": "T-77", "spn": 1, "fmi": 1}, headers=A).json()
    chaos.set("late", False)
    prompt = c.post("/sim/fault", json={"vehicle": "T-78", "spn": 2, "fmi": 1}, headers=A).json()
    assert [json.loads(b)["eventId"] for _, b, _ in sink.got] == [prompt["eventId"]]
    assert c.get("/faults/feed", headers=A).json()["items"][0]["eventId"] == prompt["eventId"]
    clock.advance(minutes=31)
    assert c.post("/sim/flush", headers=A).json()["delivered"] == 1
    assert json.loads(sink.got[-1][1])["eventId"] == late["eventId"]
    assert c.post("/sim/flush", headers=A).json()["delivered"] == 0  # delivered once


def test_receiver_down_retries(make):
    calls = {"fail": True, "n": 0}

    def flaky(url, body, headers):
        if calls["fail"]:
            raise ConnectionError
        calls["n"] += 1

    c, _ = make("telematics", sender=flaky)
    c.post("/sim/webhook", json={"url": "http://x"}, headers=A)
    c.post("/sim/fault", json={"vehicle": "T-77", "spn": 1, "fmi": 1}, headers=A)
    assert calls["n"] == 0
    calls["fail"] = False
    assert c.post("/sim/flush", headers=A).json()["delivered"] == 1


def test_dvir(make):
    c, _, sink = setup(make)
    r = c.post("/dvir", json={"vehicle": "T-77", "defect_text": "lift stuck", "reported_by_role": "operator"}, headers=A)
    eid = r.json()["eventId"]
    d = json.loads(sink.got[0][1])
    assert d["eventId"] == eid and d["dvir"]["defect_text"] == "lift stuck"
    assert c.post("/dvir", json={"vehicle": "ZZ", "defect_text": "x"}, headers=A).status_code == 404


def test_chaos_timeout(make):
    c, chaos, _ = setup(make)
    chaos.set("timeout")
    assert c.get("/faults/current", headers=A).status_code == 504
