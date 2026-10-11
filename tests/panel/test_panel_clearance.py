"""Panel: clearance items with their basis, ranking sentence, and acknowledgement (Phase 10 Step 1)."""
from fastapi.testclient import TestClient

from transitos.contracts.clearance import ClearanceItem
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
ITEMS = [
    ClearanceItem(rule="vehicle_fit", status="warn", basis="native", requires_ack=True,
                  detail="veh-2: larger class articulated used for a standard block (fallback tier)"),
    ClearanceItem(rule="operator_hours_week", status="unverifiable", basis="unknown", requires_ack=True,
                  detail="op-1: workforce does not supply hours in the 8-day window"),
    ClearanceItem(rule="vehicle_length", status="pass", basis="assumed", detail="veh-2: assumed typical length"),
    ClearanceItem(rule="operator_rest", status="not_evaluated", basis="unknown", detail="no duty data"),
]


def make_run(**kw):
    plan = Plan(decision="pull_now", reasoning="r", clearance=ITEMS, vehicle_fit="larger",
                rank_reason="plan 1 beat plan 2: 0 vs 1 unverifiable item(s)",
                steps=[Step(action="create_work_order", params={}, idempotency_key="k")])
    return RunState(run_id=RID, agency="ag1", vehicle_id="veh-1", trigger_event={}, status="AWAITING_APPROVAL",
                    plan=plan, plan_version=1, **kw)


def client(run=None):
    gw = FakeRunGateway([run or make_run()])
    audit = FakeAuditReader()
    audit.add(DecisionRecord(run_id=RID, plan_version=1, trigger_event_id="e1"))
    app = create_app(gateway=gw, crosswalk=FakeCrosswalk(), events=FakeEventSink(), audit=audit,
                     killswitch=FakeKillSwitch(), agencies=["ag1"])
    return TestClient(app), gw


def body(**kw):
    return {"decision": "approve", "plan_version": 1, "user": "pat", **kw}


def test_run_page_shows_every_non_native_item_with_its_basis_and_the_ranking():
    c, _ = client()
    html = c.get(f"/ui/runs/{RID}").text
    for frag in ("Clearance", "vehicle_fit", "operator_hours_week", "vehicle_length", "operator_rest",
                 "unverifiable", "not_evaluated", "assumed", "unknown", "native", "needs acknowledgement",
                 "plan 1 beat plan 2", "vehicle fit: larger"):
        assert frag in html, frag


def test_acknowledgement_checkboxes_for_each_requires_ack_rule():
    c, _ = client()
    html = c.get(f"/ui/runs/{RID}").text
    assert "name='acknowledged' value='vehicle_fit'" in html
    assert "name='acknowledged' value='operator_hours_week'" in html
    assert "name='acknowledged' value='operator_rest'" not in html


def test_approval_without_acknowledgement_is_refused_and_nothing_resumes():
    c, gw = client()
    r = c.post(f"/runs/{RID}/decision", json=body())
    assert r.status_code == 409 and "vehicle_fit" in r.json()["detail"] and "operator_hours_week" in r.json()["detail"]
    r = c.post(f"/runs/{RID}/decision", json=body(acknowledged=["vehicle_fit"]))
    assert r.status_code == 409 and "operator_hours_week" in r.json()["detail"] and "vehicle_fit" not in r.json()["detail"]
    assert gw.resume_calls == []


def test_acknowledged_approval_goes_through_and_records_the_acknowledgement():
    c, gw = client()
    r = c.post(f"/runs/{RID}/decision", json=body(acknowledged=["vehicle_fit", "operator_hours_week"]))
    assert r.status_code == 200 and len(gw.resume_calls) == 1
    assert gw.runs[RID].acknowledged == ["operator_hours_week", "vehicle_fit"]


def test_reject_never_needs_acknowledgement():
    c, _ = client()
    assert c.post(f"/runs/{RID}/decision", json=body(decision="reject")).status_code == 200


def test_html_form_posts_the_ticked_rules():
    c, gw = client()
    r = c.post(f"/ui/runs/{RID}/decision",
               content="decision=approve&plan_version=1&user=pat&acknowledged=vehicle_fit&acknowledged=operator_hours_week",
               headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 200 and "Recorded approve" in r.text and len(gw.resume_calls) == 1
    c2, gw2 = client()
    r = c2.post(f"/ui/runs/{RID}/decision", content="decision=approve&plan_version=1&user=pat&acknowledged=vehicle_fit",
                headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 409 and "Not applied" in r.text and gw2.resume_calls == []


def test_summary_lists_what_still_needs_acknowledging():
    c, _ = client()
    (p,) = c.get("/pending").json()
    assert p["needs_acknowledgement"] == ["operator_hours_week", "vehicle_fit"]


def test_plan_with_only_native_passes_says_so():
    plan = Plan(decision="pull_now", reasoning="r", steps=[Step(action="create_work_order", params={}, idempotency_key="k")])
    run = RunState(run_id=RID, agency="ag1", vehicle_id="veh-1", trigger_event={}, status="AWAITING_APPROVAL",
                   plan=plan, plan_version=1)
    c, _ = client(run)
    assert "All rules passed on native data" in c.get(f"/ui/runs/{RID}").text
    assert c.post(f"/runs/{RID}/decision", json=body()).status_code == 200
