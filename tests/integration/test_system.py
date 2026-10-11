"""End-to-end through the REAL wiring (mock products -> watch adapters -> outbox -> bus -> crosswalk ->
starter -> LangGraph -> Track C adapters -> mocks; panel over real ports), beyond F's scenario set:
the telematics webhook path, process restart, replay, self-inflicted updates, unresolved-ID fix."""
from __future__ import annotations

import pytest

from transitos.contracts.envelope import TOPIC_RAW, TOPIC_RESOLVED
from transitos.integration.world import IntegrationWorld
from transitos.verification.invariants import check_all
from transitos.verification.scenarios import AGENCY, BY_ID

H = {"X-Agency": AGENCY}


class Crash(BaseException):
    """Process killed (not an Exception: nothing in the system may swallow it)."""


@pytest.fixture
def world():
    w = IntegrationWorld()
    w.apply(BY_ID["operator_swap_needed"])
    return w


def fault_via_mock(w: IntegrationWorld) -> str:
    """A sensor fault raised in the telematics mock reaches the system only via its signed webhook."""
    r = w.mocks.clients["telematics"].post("/sim/fault", headers=H,
                                           json={"vehicle": "T-77", "spn": 520, "fmi": 3, "lamps": {"stop": True}})
    r.raise_for_status()
    w.system.run_until_idle()
    (run,) = w.system.store.list_runs()
    return run["run_id"]


def test_webhook_to_done_through_real_ports(world):
    s = world.system
    rid = fault_via_mock(world)
    (resolved,) = [e for _, _, e in s.bus.messages(TOPIC_RESOLVED) if e.type == "VehicleFaultDetected"]
    assert resolved.canonicalid == "veh-1" and resolved.data["refs"]["scheduling"] == "BUS-44"
    pending = s.panel_client.get("/pending", params={"agency": AGENCY}).json()
    assert [p["run_id"] for p in pending] == [rid] and s.writes.records == []
    world.approve(rid)
    assert [w.action for w in s.writes.records] == ["create_work_order", "set_vehicle_unavailable",
                                                   "reassign_block", "assign_operator", "publish_rider_alert"]
    world.advance_clock(48)
    world.close_work_order(rid)
    assert s.gateway.get_run(rid).status == "RETURNING"
    world.approve(rid, scope="return_to_service")
    run = s.gateway.get_run(rid)
    assert run.status == "DONE" and s.writes.records[-1].action == "set_vehicle_available"
    assert check_all(world.observe()) == []
    # append-only audit: every revision kept, UPDATE/DELETE refused by the store
    assert len(s.decisions.list_for_run(rid)) > 3
    with pytest.raises(Exception, match="append-only"):
        s.db.execute("DELETE FROM decisions")
    hist = s.panel_client.get(f"/runs/{rid}").json()["history"]
    assert hist[-1]["outcome"] == "done" and {a["kind"] for a in hist[-1]["actions"]} == {"write"}


def test_our_own_writes_do_not_invalidate_the_plan(world):
    s = world.system
    rid = fault_via_mock(world)
    world.approve(rid)
    ours = [e for _, _, e in s.bus.messages(TOPIC_RAW) if e.type in ("VehicleStatusChanged", "WorkOrderOpened")]
    assert ours and all(e.data.get("changed_by", "").startswith("transitos:") or e.data.get("opened_by") == "system"
                        for e in ours)
    seen = {r["event_id"]: r["action"] for r in s.store.seen_events()}
    assert all(seen[e.id] == "ignored" for e in ours)


def test_process_restart_mid_execution_resumes_without_repeating_writes(world):
    s = world.system
    rid = fault_via_mock(world)

    def crash(action, key):
        if action == "reassign_block":
            s.writes.crash_before = None
            raise Crash(action)
    s.writes.crash_before = crash
    run = s.gateway.get_run(rid)
    with pytest.raises(Crash):  # killed inside the approval's graph invocation, after two writes
        s.gateway.submit_decision(rid, "approve", run.plan_version, None, "dispatcher")
    assert [w.action for w in s.writes.records] == ["create_work_order", "set_vehicle_unavailable"]

    world.restart()  # new connections, graph, stores from the same SQLite files; recover() re-drives the run
    acts = [w.action for w in s.writes.records]
    assert acts == ["create_work_order", "set_vehicle_unavailable", "reassign_block", "assign_operator",
                    "publish_rider_alert"]
    assert s.gateway.get_run(rid).status == "WAITING_FOR_REPAIR"
    wos = world.mocks.clients["maintenance"].get("/workorders", headers=H).json()
    assert len(wos) == 1


def test_restart_while_awaiting_approval_then_approve_via_new_panel(world):
    rid = fault_via_mock(world)
    world.restart()
    assert world.system.gateway.get_run(rid).status == "AWAITING_APPROVAL"
    world.approve(rid)
    assert world.system.gateway.get_run(rid).status == "WAITING_FOR_REPAIR"


def test_replaying_resolved_events_creates_no_new_runs_or_writes(world):
    s = world.system
    rid = fault_via_mock(world)
    world.approve(rid)
    n_writes, n_runs = len(s.writes.records), len(s.store.list_runs())
    s.bus.reset_group(TOPIC_RESOLVED, "workflow", 0)
    s.bus.reset_group(TOPIC_RAW, "crosswalk", 0)
    s.run_until_idle()
    assert len(s.writes.records) == n_writes and len(s.store.list_runs()) == n_runs


def test_unresolved_vehicle_parked_then_fixed_in_panel_starts_run(world):
    s = world.system
    # a telematics id nobody mapped: simulate a new vehicle unit by delivering its raw event directly
    from transitos.verification.scenarios import fault_event
    world.deliver(fault_event(native="T-999"))
    unresolved = s.panel_client.get("/unresolved", params={"agency": AGENCY}).json()
    assert [u["native_id"] for u in unresolved if u["native_id"] == "T-999"] == ["T-999"]
    # map it to veh-2 (a spare) via the panel: parked event is republished and starts a run
    r = s.panel_client.post("/crosswalk", json={"agency": AGENCY, "product": "telematics", "native_id": "T-999",
                                                "entity": "vehicle", "canonical_id": "veh-2"})
    assert r.status_code == 200 and r.json()["republished"]
    s.run_until_idle()
    assert "veh-2" in {x["vehicle"] for x in s.store.list_runs()}
    assert not [u for u in s.panel_client.get("/unresolved").json() if u["native_id"] == "T-999"]


def test_panel_kill_switch_blocks_adapter_writes_live(world):
    s = world.system
    rid = fault_via_mock(world)
    world.set_kill_switch(True)
    world.approve(rid)
    assert s.writes.records == [] and "kill_switch" in s.gateway.get_run(rid).violations


def _fail_alert(world):
    from contextlib import contextmanager

    @contextmanager
    def around(action, key, comp):
        on = action == "publish_rider_alert"
        if on:
            world.mocks.chaos["alerts"].set("timeout")
        try:
            yield
        finally:
            if on:
                world.mocks.chaos["alerts"].set("timeout", False)
    world.system.writes.around = around


def test_rider_alert_failure_keeps_safety_steps_and_escalates(world):
    """The irreversible last step failed: no compensation may follow an alert attempt (Track F's
    rider_alert_last); the vehicle stays pulled, the block stays on the spare, a human takes over."""
    _fail_alert(world)
    rid = fault_via_mock(world)
    world.approve(rid)
    esc = world.system.store.get_escalation(rid)
    assert esc["reason"].startswith("irreversible_step_failed:publish_rider_alert")
    assert esc["still_applied"] == ["create_work_order", "set_vehicle_unavailable", "reassign_block:blk-1",
                                    "assign_operator:blk-1"]
    assert not [w for w in world.system.writes.records if w.compensation]
    assert check_all(world.observe()) == []


def _reject_assign(world):
    from contextlib import contextmanager

    @contextmanager
    def around(action, key, comp):
        on = action == "assign_operator" and not comp
        if on:
            world.mocks.chaos["workforce"].set("reject_write")
        try:
            yield
        finally:
            if on:
                world.mocks.chaos["workforce"].set("reject_write", False)
    world.system.writes.around = around


def _state(world):
    docs = {d['id']: d for d in world._docs('scheduling', 'vehicles')}
    blocks = {b['id']: b for b in world._docs('scheduling', 'blocks')}
    return docs, blocks


def test_failure_after_reassign_compensates_every_step_in_reverse(world):
    """A57 (resolves A48): compensating reassign_block is a restore of the prior assignment, which the
    scheduling product accepts although the faulted vehicle is out of service. assign_operator fails ->
    full reverse undo, consistent end state, every compensation audited."""
    before_vehicles, before_blocks = _state(world)
    _reject_assign(world)
    rid = fault_via_mock(world)
    world.approve(rid)
    comps = [w for w in world.system.writes.records if w.compensation and w.ok]
    assert [c.action for c in comps] == ["reassign_block", "set_vehicle_available", "cancel_work_order"]
    assert all(c.compensates_key for c in comps)
    assert world.system.store.get_escalation(rid)["reason"] != "compensation_failed"
    vehicles, blocks = _state(world)
    assert {b: d["vehicle"] for b, d in blocks.items()} == {b: d["vehicle"] for b, d in before_blocks.items()}
    norm = {"in_service": "ready"}  # the product's "available" call sets ready (the mock's notion of available)
    assert {v: norm.get(d["status"], d["status"]) for v, d in vehicles.items()} == \
        {v: norm.get(d["status"], d["status"]) for v, d in before_vehicles.items()}
    assert not [v for v in vehicles.values() if v["status"] == "out_of_service"]
    # every compensation is in the append-only audit (decision store) with its key
    audited = {a["key"] for rec in world.system.decisions.list_for_run(rid) for a in rec.actions if a.get("key")}
    assert {c.key for c in comps} <= audited
    assert check_all(world.observe()) == []


def test_normal_assignment_to_unavailable_vehicle_still_refused(world):
    """Regression: the restore exception must not let a NEW assignment onto an unavailable vehicle through."""
    rid = fault_via_mock(world)
    world.approve(rid)  # block now on the spare, faulted vehicle out of service
    vehicles, blocks = _state(world)
    (oos,) = [v for v in vehicles.values() if v["status"] == "out_of_service"]
    (blk,) = [b for b in blocks.values() if b["vehicle"] != oos["id"]][:1]
    c = world.mocks.clients["scheduling"]
    r = c.post(f"/blocks/{blk['id']}/reassign", headers=H, json={"vehicle": oos["id"]})
    assert r.status_code == 409 and "out of service" in r.text
