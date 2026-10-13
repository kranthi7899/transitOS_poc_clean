import time

import pytest
from fastapi.testclient import TestClient

from transitos.integration.console import create_app
from transitos.workflow.memory import IncidentMemory
from transitos.workflow.reasoning import StubReasoner

H = {"X-TransitOS-Demo": "1"}


@pytest.fixture
def console(tmp_path):
    with TestClient(create_app(StubReasoner(), IncidentMemory(tmp_path / "memory.sqlite"))) as client:
        yield client


def state(c):
    return c.get("/api/demo/state").json()


def act(c, action, **kw):
    before = state(c)
    run = next(iter(before["runs"]), {})
    response = c.post("/api/demo/command", headers=H, json={
        "action": action, "generation": before["generation"], "plan_version": run.get("plan_version"), **kw})
    assert response.status_code == 202, response.text
    deadline = time.monotonic() + 5
    while state(c)["busy"] and time.monotonic() < deadline:
        time.sleep(0.01)
    after = state(c)
    assert not after["busy"] and not after["error"], after["error"]
    return after


def test_console_real_fault_to_separate_return_approval(console):
    assert console.get("/").status_code == 200
    s = act(console, "trigger")
    assert s["runs"][0]["status"] == "AWAITING_APPROVAL"
    assert s["writes"] == []
    assert any(i["status"] == "pass" for i in s["assessment"]["items"])
    assert {"gather", "check", "reason", "await_approval", "interrupt"} <= {r["node"] for r in s["trace"]}
    s = act(console, "approve", user="Demo dispatcher")
    assert s["runs"][0]["status"] == "WAITING_FOR_REPAIR"
    assert [w["action"] for w in s["writes"]] == [
        "create_work_order", "set_vehicle_unavailable", "reassign_block", "assign_operator", "publish_rider_alert"]
    assert s["visible_alerts"] == []
    assert act(console, "advance")["visible_alerts"]
    s = act(console, "repair")
    assert s["runs"][0]["status"] == "RETURNING"
    assert next(v for v in s["vehicles"] if v["id"] == "veh-1")["status"] == "out_of_service"
    s = act(console, "approve", scope="return_to_service")
    assert s["runs"][0]["status"] == "DONE"
    assert s["invariant_violations"] == []
    assert s["writes"][-1]["action"] == "set_vehicle_available"
    assert len(s["history"]) >= 5


def test_rejection_and_reset_do_not_reuse_old_decision(console):
    old = act(console, "trigger")
    s = act(console, "reject")
    assert s["writes"] == [] and s["runs"][0]["status"] == "CLOSED_REJECTED"
    fresh = act(console, "reset")
    assert fresh["generation"] != old["generation"] and not fresh["runs"] and not fresh["trace"]
    response = console.post("/api/demo/command", headers=H, json={
        "action": "approve", "generation": old["generation"], "plan_version": 1})
    assert response.status_code == 409


def test_stale_approval_and_pause_are_blocked_before_writes(console):
    s = act(console, "trigger")
    response = console.post("/api/demo/command", headers=H, json={
        "action": "approve", "generation": s["generation"], "plan_version": 999})
    assert response.status_code == 409 and state(console)["writes"] == []
    act(console, "pause")
    response = console.post("/api/demo/command", headers=H, json={
        "action": "approve", "generation": s["generation"], "plan_version": 1})
    assert response.status_code == 409 and state(console)["writes"] == []
    act(console, "resume")
    assert act(console, "approve")["runs"][0]["status"] == "WAITING_FOR_REPAIR"


def test_mutations_require_same_origin_and_custom_header(console):
    body = {"action": "trigger", "generation": state(console)["generation"]}
    assert console.post("/api/demo/command", json=body).status_code == 403
    assert console.post("/api/demo/command", headers={**H, "Origin": "https://foreign.example"}, json=body).status_code == 403
    assert console.get("/", headers={"Host": "foreign.example"}).status_code == 403
    assert state(console)["runs"] == []


def test_duplicate_approval_does_not_repeat_effects(console):
    s = act(console, "trigger")
    act(console, "approve")
    response = console.post("/api/demo/command", headers=H, json={
        "action": "approve", "generation": s["generation"], "plan_version": 1})
    assert response.status_code == 409 and len(state(console)["writes"]) == 5


def test_unknown_safety_data_requires_explicit_acknowledgement(console):
    session = console.app.state.demo
    session.world.mocks.clients["workforce"].post("/sim/operator-fields", headers=session.world._h,
        json={"drop": ["medical_valid_to", "endorsements"]}).raise_for_status()
    s = act(console, "trigger")
    required = sorted({i["rule"] for i in s["runs"][0]["plan"]["clearance"] if i["requires_ack"]})
    assert required
    response = console.post("/api/demo/command", headers=H, json={
        "action": "approve", "generation": s["generation"], "plan_version": 1})
    assert response.status_code == 409 and state(console)["writes"] == []
    assert act(console, "approve", acknowledged=required)["runs"][0]["status"] == "WAITING_FOR_REPAIR"
