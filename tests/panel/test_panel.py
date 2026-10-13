from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from transitos.contracts.envelope import CloudEvent
from transitos.contracts.objects import DecisionRecord, Plan, RunState, Step
from transitos.panel.app import create_app
from transitos.panel.fakes import (
    FakeAuditReader,
    FakeCrosswalk,
    FakeEventSink,
    FakeKillSwitch,
    FakeRunGateway,
)

RID = "ag1:veh-1:fault:ag1:T-1:100:1:t0"


def make_run(version=1):
    plan = Plan(decision="pull_now", reasoning="stop lamp <b>on</b>",
                steps=[Step(action="create_work_order", params={"a": 1}, idempotency_key="k")])
    return RunState(run_id=RID, agency="ag1", vehicle_id="veh-1", trigger_event={},
                    status="AWAITING_APPROVAL", plan=plan, plan_version=version,
                    facts={"spare": "veh-9"})


@pytest.fixture
def env():
    gw = FakeRunGateway([make_run()])
    cw, sink, audit, ks = FakeCrosswalk(), FakeEventSink(), FakeAuditReader(), FakeKillSwitch()
    audit.add(DecisionRecord(run_id=RID, plan_version=1, trigger_event_id="e1"))
    audit.entries.append({"time": "t", "run_id": RID, "kind": "proposed", "summary": "s", "agency": "ag1"})
    app = create_app(gateway=gw, crosswalk=cw, events=sink, audit=audit, killswitch=ks, agencies=["ag1"])
    return TestClient(app), gw, cw, sink, ks


def dec(version=1, decision="approve"):
    return {"decision": decision, "plan_version": version, "user": "pat"}


def test_pending_and_detail(env):
    c, *_ = env
    assert [p["run_id"] for p in c.get("/pending", params={"agency": "ag1"}).json()] == [RID]
    assert c.get("/pending", params={"agency": "zz"}).json() == []
    d = c.get(f"/runs/{RID}").json()
    assert d["plan"]["decision"] == "pull_now" and d["facts"] == {"spare": "veh-9"}
    assert len(d["history"]) == 1
    assert c.get("/runs/nope").status_code == 404


def test_stale_plan_version_rejected(env):
    c, gw, *_ = env
    r = c.post(f"/runs/{RID}/decision", json=dec(version=0))
    assert r.status_code == 409
    assert gw.resume_calls == []


def test_decision_resumes_exactly_once(env):
    c, gw, *_ = env
    assert c.post(f"/runs/{RID}/decision", json=dec()).status_code == 200
    r2 = c.post(f"/runs/{RID}/decision", json=dec())
    assert r2.status_code == 200 and r2.json()["already_decided"] is True
    assert len(gw.resume_calls) == 1
    assert c.post(f"/runs/{RID}/decision", json=dec(decision="reject")).status_code == 409
    assert len(gw.resume_calls) == 1


def test_defect_creates_human_event(env):
    c, _, _, sink, _ = env
    r = c.post("/defects", json={"agency": "ag1", "native_vehicle_id": "T-77", "defect_text": "brakes soft"})
    assert r.status_code == 202
    ev = sink.events[0]
    assert ev.type == "DefectReported" and ev.data["source"] == "human"
    assert ev.data["native_vehicle_id"] == "T-77"
    c.post("/defects", json={"agency": "ag1", "native_vehicle_id": "T-77", "defect_text": "x",
                             "spn": 100, "fmi": 1})
    assert sink.events[1].type == "VehicleFaultDetected" and sink.events[1].data["source"] == "human"
    CloudEvent.model_validate(sink.events[1].model_dump())


def test_unresolved_fix_republishes_parked(env):
    c, _, cw, sink, _ = env
    ev = CloudEvent(id="f1", source="adapter/telematics/ag1", type="VehicleFaultDetected",
                    time=datetime.now(UTC), agencyid="ag1",
                    data={"native_vehicle_id": "T-9", "spn": 1, "fmi": 1})
    cw.park(ev, "telematics", "T-9")
    assert c.get("/unresolved").json()[0]["native_id"] == "T-9"
    r = c.post("/crosswalk", json={"agency": "ag1", "product": "telematics", "native_id": "T-9",
                                   "entity": "vehicle", "canonical_id": "veh-9"})
    assert r.json()["republished"] == ["f1"]
    assert [e.id for e in sink.events] == ["f1"]
    assert cw.lookup("ag1", "telematics", "T-9") == "veh-9"
    assert c.get("/unresolved").json() == []


def test_killswitch_toggles(env):
    c, *_, ks = env
    assert c.get("/killswitch", params={"agency": "ag1"}).json()["on"] is False
    assert c.post("/killswitch", json={"agency": "ag1", "on": True}).json()["on"] is True
    assert ks.is_on("ag1")
    c.post("/killswitch", json={"agency": "ag1", "on": False})
    assert not ks.is_on("ag1")


def test_html_pages_render(env):
    c, *_ = env
    for path in ["/ui/pending", f"/ui/runs/{RID}", "/ui/unresolved", "/ui/killswitch",
                 "/ui/timeline", "/ui/defect"]:
        r = c.get(path)
        assert r.status_code == 200 and "viewport" in r.text, path
    page = c.get(f"/ui/runs/{RID}").text
    assert "Approve" in page and "Reject" in page and "Edit" in page
    assert "&lt;b&gt;on" in page  # escaped
    assert "veh-1" in c.get("/ui/pending").text


def test_html_form_decision_and_killswitch(env):
    c, gw, _, _, ks = env
    r = c.post(f"/ui/runs/{RID}/decision", content="decision=approve&plan_version=1&user=pat&edits=",
               headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 200 and len(gw.resume_calls) == 1
    stale = c.post(f"/ui/runs/{RID}/decision", content="decision=reject&plan_version=0&user=pat",
                   headers={"content-type": "application/x-www-form-urlencoded"})
    assert stale.status_code == 409
    c.post("/ui/killswitch", content="agency=ag1&on=true", follow_redirects=False,
           headers={"content-type": "application/x-www-form-urlencoded"})
    assert ks.is_on("ag1")


def test_return_to_service_approval_is_a_separate_scope(env):
    c, gw, *_ = env
    body = {**dec(), "scope": "return_to_service"}
    assert c.post(f"/runs/{RID}/decision", json=body).status_code == 409   # not awaiting return yet
    gw.runs[RID].status, gw.runs[RID].decision = "RETURNING", "approve"   # plan approved earlier
    assert "Approve return" in c.get(f"/ui/runs/{RID}").text
    r = c.post(f"/runs/{RID}/decision", json=body)
    assert r.status_code == 200 and r.json()["status"] == "DONE" and r.json()["already_decided"] is False
    again = c.post(f"/runs/{RID}/decision", json=body)
    assert again.status_code == 200 and again.json()["already_decided"] is True
    assert [x[1] for x in gw.resume_calls] == ["return:approve"]
    assert c.post(f"/runs/{RID}/decision", json={**body, "decision": "edit"}).status_code == 422


def test_return_approval_rejects_stale_version_before_resuming(env):
    c, gw, *_ = env
    gw.runs[RID].status = "RETURNING"
    response = c.post(f"/runs/{RID}/decision", json={**dec(version=0), "scope": "return_to_service"})
    assert response.status_code == 409 and gw.resume_calls == []


def test_defect_event_time_uses_injected_clock():
    t = datetime(2026, 10, 10, 6, 0, tzinfo=UTC)
    sink = FakeEventSink()
    app = create_app(gateway=FakeRunGateway(), crosswalk=FakeCrosswalk(), events=sink, audit=FakeAuditReader(),
                     killswitch=FakeKillSwitch(), clock=lambda: t)
    TestClient(app).post("/defects", json={"agency": "ag1", "native_vehicle_id": "T-77", "defect_text": "x"})
    assert sink.events[0].time == t


def test_conflicts_are_listed_read_only_in_the_api_and_the_unresolved_page():
    items = [{"agency": "ag1", "product": "maintenance", "kind": "native_id_shared",
              "native_ids": ["M-9"], "canonical_ids": ["veh-1", "veh-2"]}]
    seen = []
    app = create_app(gateway=FakeRunGateway(), crosswalk=FakeCrosswalk(), events=FakeEventSink(),
                     audit=FakeAuditReader(), killswitch=FakeKillSwitch(),
                     conflicts=lambda ag: seen.append(ag) or items)
    c = TestClient(app)
    assert c.get("/conflicts", params={"agency": "ag1"}).json() == items and seen == ["ag1"]
    page = c.get("/ui/unresolved").text
    assert "Conflicting ID mappings" in page and "M-9" in page and "veh-1, veh-2" in page
    quiet = TestClient(create_app(gateway=FakeRunGateway(), crosswalk=FakeCrosswalk(), events=FakeEventSink(),
                                  audit=FakeAuditReader(), killswitch=FakeKillSwitch()))
    assert quiet.get("/conflicts").json() == [] and "Conflicting ID mappings" not in quiet.get("/ui/unresolved").text
