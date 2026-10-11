"""The agency-042 seed (the mocks' default fleet) completes a run (Phase 10 Step 1; A26 and A49 closed).

Before: operator qualifications were licence codes while the check expected the vehicle type, and the workflow
spent one distinct spare per affected block, so eight sequential blocks hit the last-spare rule. Now: licence
class / endorsement / trained types with dates, and a spare may serve several non-overlapping blocks."""
from __future__ import annotations

import pytest

from transitos.contracts.tools import SAGA_ORDER
from transitos.integration.system import Mocks, System
from transitos.mocks.clock import SimClock
from transitos.verification.invariants import check_all

AG = "agency-042"
H = {"X-Agency": AG}


def approve_run(s, rid, scope="plan", acknowledged=()):
    cur = s.gateway.get_run(rid)
    r = s.panel_client.post(f"/runs/{rid}/decision", json={
        "decision": "approve", "plan_version": cur.plan_version, "user": "dispatcher", "scope": scope,
        "acknowledged": list(acknowledged)})
    s.run_until_idle()
    return r


def raise_fault(s):
    r = s.mocks.clients["telematics"].post("/sim/fault", headers=H, json={
        "vehicle": "T-77", "spn": 520, "fmi": 3, "lamps": {"stop": True}})
    r.raise_for_status()
    s.run_until_idle()
    (run_row,) = s.store.list_runs()
    return run_row["run_id"]


@pytest.fixture
def system(tmp_path):
    mocks = Mocks.start(SimClock())
    mocks.seed(AG)
    return System(AG, mocks, tmp_path)


def test_agency_042_severe_fault_completes_with_five_saga_steps(system, monkeypatch):
    s = system
    r = s.mocks.clients["telematics"].post("/sim/fault", headers=H, json={
        "vehicle": "T-77", "spn": 520, "fmi": 3, "lamps": {"stop": True}})
    r.raise_for_status()
    s.run_until_idle()
    (run_row,) = s.store.list_runs()
    rid = run_row["run_id"]
    run = s.gateway.get_run(rid)
    assert run.status == "AWAITING_APPROVAL", (run.status, run.violations)
    plan = run.plan
    assert {st.action for st in plan.steps} == set(SAGA_ORDER)         # all five saga step types
    reassigns = [st for st in plan.steps if st.action == "reassign_block"]
    assert len(reassigns) == 8 and len({st.params["to_vehicle_id"] for st in reassigns}) <= 2   # spares reused
    assert s.writes.records == []                                       # nothing written before approval
    # F13: the base fleet supplies every safety/legal field, so NO acknowledgement is required (exact set)
    assert {i.rule for i in plan.clearance if i.requires_ack} == set()

    def approve(scope="plan"):   # acknowledges nothing: the plan must not need it
        approve_run(s, rid, scope=scope, acknowledged=[])

    approve()
    done = [w.action for w in s.writes.records if w.ok and not w.compensation]
    order = [a for a in done if a != "set_vehicle_available"]
    assert order == sorted(order, key=SAGA_ORDER.index) and order[-1] == "publish_rider_alert"
    assert set(order) == set(SAGA_ORDER)
    s.mocks.clock.advance(hours=72)
    s.mocks.clients["telematics"].post("/sim/flush", headers=H)
    s.mocks.clients["maintenance"].post(f"/sim/workorders/{s.gateway.get_run(rid).wo_id}/close", headers=H)
    s.run_until_idle()
    approve("return_to_service")
    final = s.gateway.get_run(rid)
    assert final.status == "DONE", (final.status, final.violations)
    # the outcome tripwire holds on the real final numbers
    from transitos.integration import world as world_mod
    from transitos.integration.world import IntegrationWorld
    monkeypatch.setattr(world_mod, "AGENCY", AG)      # the observation helper is written for the scenario agency
    w = IntegrationWorld()
    w.system, w.mocks = s, s.mocks
    obs = w.observe()
    assert [v for v in check_all(obs) if v.invariant == "hard_limits"] == []
    assert {w.params["operator_id"] for w in obs.writes if w.action == "assign_operator"} == {"op-10"}


def test_agency_042_run_needing_an_acknowledgement_does_not_execute_until_it_is_given(system):
    """F13: the same severe fault when the workforce export leaves weekly hours blank for every operator. The
    plan stays on the list but requires exactly one acknowledgement (operator_hours_week); approving without
    it writes nothing; approving with it runs the saga."""
    s = system
    s.mocks.clients["workforce"].post("/sim/operator-fields", headers=H, json={"blank": ["hours_week"]}
                                      ).raise_for_status()
    rid = raise_fault(s)
    run = s.gateway.get_run(rid)
    assert run.status == "AWAITING_APPROVAL", (run.status, run.violations)
    assert {i.rule for i in run.plan.clearance if i.requires_ack} == {"operator_hours_week"}
    r = approve_run(s, rid, acknowledged=[])
    assert r.status_code == 409 and "operator_hours_week" in r.json()["detail"]
    assert s.writes.records == [] and s.gateway.get_run(rid).status == "AWAITING_APPROVAL"
    approve_run(s, rid, acknowledged=["operator_hours_week"])
    done = [w.action for w in s.writes.records if w.ok and not w.compensation]
    assert set(done) == set(SAGA_ORDER) and done[-1] == "publish_rider_alert"
    assert s.gateway.get_run(rid).acknowledged == ["operator_hours_week"]
