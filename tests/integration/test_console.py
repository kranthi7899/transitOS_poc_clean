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


def native_record(s, product, path):
    return s["product_records"][product]["records"][path]


def test_native_product_records_confirm_changes_and_separate_return(console):
    before = state(console)
    assert native_record(before, "scheduling", "/blocks")[0]["vehicle"] == "BUS-44"
    baseline = before["product_baseline"]
    assert native_record(before, "workforce", "/export/operators.csv")["format"] == "text/csv"
    proposal = act(console, "trigger")
    assert native_record(proposal, "telematics", "/faults/current")[0]["vehicle"]["id"] == "T-77"
    assert native_record(proposal, "maintenance", "/workorders") == []
    recovery = act(console, "approve")
    assert native_record(recovery, "scheduling", "/blocks")[0]["vehicle"] == "BUS-45"
    assert native_record(recovery, "workforce", "/assignments/blk-1")["operator"] == "OP-2"
    assert native_record(recovery, "maintenance", "/workorders")[0]["asset"] == "1106"
    assert native_record(recovery, "alerts", "/alerts") == []
    assert native_record(act(console, "advance"), "alerts", "/alerts")
    returning = act(console, "repair")
    assert native_record(returning, "maintenance", "/workorders")[0]["status"] == "closed"
    assert next(v for v in native_record(returning, "scheduling", "/vehicles") if v["id"] == "BUS-44")["status"] == "out_of_service"
    done = act(console, "approve", scope="return_to_service")
    assert next(v for v in native_record(done, "scheduling", "/vehicles") if v["id"] == "BUS-44")["status"] == "ready"
    assert native_record(done, "scheduling", "/blocks")[0]["vehicle"] == "BUS-45"
    assert done["product_baseline"] == baseline
    # UI evidence is the same record returned directly by the actual independent mock application.
    session = console.app.state.demo
    assert native_record(done, "scheduling", "/blocks") == session.world.mocks.clients["scheduling"].get("/blocks", headers=session.world._h).json()


def test_product_workspaces_are_read_only_and_notifications_do_not_duplicate(console):
    for product in ("telematics", "maintenance", "scheduling", "workforce", "alerts"):
        assert console.get(f"/products/{product}").status_code == 200
    assert console.get("/products/unknown").status_code == 404
    assert state(console)["writes"] == [] and state(console)["notifications"] == []
    act(console, "trigger")
    recovery = act(console, "approve")
    titles = {n["title"] for n in recovery["notifications"]}
    assert {"Replacement bus assignment confirmed", "Workflow waiting for repair", "Rider alert published"} <= titles
    assert not any("arrived" in title.lower() for title in titles)
    ids = [n["key"] for n in recovery["notifications"]]
    assert len(ids) == len(set(ids))
    session = console.app.state.demo
    session.publish()
    assert state(console)["notifications"] == recovery["notifications"]
    for _ in range(3):
        assert state(console)["runs"][0]["status"] == "WAITING_FOR_REPAIR"
    returning = act(console, "repair")
    assert any(n["title"] == "Return-to-service approval needed" for n in returning["notifications"])
    assert len(returning["writes"]) == 5
    assert act(console, "reset")["notifications"] == []


def test_identity_bridge_uses_real_crosswalk_and_resolved_event(console):
    s = state(console)
    groups = {g["canonical"]: g for g in s["identity_bridge"]["groups"]}
    assert groups["veh-1"]["aliases"] == {"scheduling": "BUS-44", "telematics": "T-77", "maintenance": "1106"}
    assert groups["op-2"]["aliases"] == {"workforce": "OP-2"}
    assert all(g["status"] == "mapped" for g in groups.values())
    assert all(r["usable"] and r["agency"] == s["agency"] for g in groups.values() for r in g["rows"])
    assert not s["identity_bridge"]["events"]
    proposal = act(console, "trigger")
    fault = next(e for e in proposal["identity_bridge"]["events"] if e["type"] == "VehicleFaultDetected")
    assert fault["native"] == "T-77" and fault["canonical"] == "veh-1"
    assert fault["refs"] == {"canonical": "veh-1", "telematics": "T-77", "maintenance": "1106", "scheduling": "BUS-44"}
    assert proposal["writes"] == []


def test_identity_bridge_never_selects_a_conflicting_native_alias(console):
    from transitos.contracts.crosswalk import CrosswalkEntry
    session = console.app.state.demo
    session.system.xw.resolve(CrosswalkEntry(agency=session.system.agency, product="telematics",
        native_id="T-77", entity="vehicle", canonical_id="veh-2", valid_from=session.system.clock.now()))
    session.publish()
    bridge = state(console)["identity_bridge"]
    assert bridge["conflicts"]
    incident = next(g for g in bridge["groups"] if g["canonical"] == "veh-1")
    assert incident["status"] == "review_required" and "telematics" not in incident["aliases"]
    assert any(r["native_id"] == "T-77" and not r["usable"] for r in incident["rows"])
    assert state(console)["writes"] == []
