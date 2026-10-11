"""Step 1 on the real integrated system: decision record in the append-only audit store, panel over real ports,
capability profiles from the real adapters, mock switch for dropped operator fields."""
from __future__ import annotations

from transitos.integration.world import IntegrationWorld
from transitos.verification.runner import drive
from transitos.verification.scenarios import BY_ID


def run_scenario_world(sid: str):
    w = IntegrationWorld()
    obs = drive(w, BY_ID[sid])
    return w, obs, obs.runs[0]


def test_audit_store_keeps_clearance_items_acknowledgement_and_fit_setting():
    w, _, run = run_scenario_world("larger_class_fallback")
    recs = w.system.decisions.list_for_run(run.run_id)
    last = recs[-1]
    assert last.vehicle_fit == "allow_larger" and last.acknowledged == ["vehicle_fit"]
    assert [c["rule"] for c in last.clearance if c["status"] == "warn"] == ["vehicle_fit"]
    assert all(not (c["status"] == "pass" and c["basis"] == "native") for c in last.clearance)
    assert last.outcome == "done"


def test_decision_record_for_missing_data_lists_every_unverifiable_and_not_evaluated_item():
    w, _, run = run_scenario_world("half_operator_fields_missing")
    last = w.system.decisions.list_for_run(run.run_id)[-1]
    by_status = {}
    for c in last.clearance:
        by_status.setdefault(c["status"], set()).add(c["rule"])
    assert by_status["unverifiable"] == {"operator_qualification", "operator_hours_week", "operator_medical"}
    assert {"operator_rest", "operator_spread"} <= by_status["not_evaluated"]   # medical is legal (F8)
    assert set(last.acknowledged) == by_status["unverifiable"]


def test_panel_page_of_the_real_run_shows_items_basis_and_ranking():
    w = IntegrationWorld()
    sc = BY_ID["half_operator_fields_missing"]
    w.apply(sc)
    w.deliver(sc.events[0])
    rid = w.observe().runs[0].run_id
    html = w.system.panel_client.get(f"/ui/runs/{rid}").text
    for frag in ("operator_qualification", "unverifiable", "unknown", "needs acknowledgement",
                 "name='acknowledged' value='operator_hours_week'", "Ranking"):
        assert frag in html, frag
    r = w.system.panel_client.post(f"/runs/{rid}/decision", json={
        "decision": "approve", "plan_version": 1, "user": "pat", "acknowledged": ["operator_qualification"]})
    assert r.status_code == 409 and "operator_hours_week" in r.json()["detail"]
    assert w.observe().writes == []


def test_real_adapters_report_what_they_supply_and_the_switch_removes_columns():
    w = IntegrationWorld()
    w.apply(BY_ID["half_operator_fields_missing"])
    prof = w.system.toolset.capability_profiles("agency-a").value
    f = prof["workforce"]["fields"]
    assert f["licence_class"] == "unknown" and f["hours_week"] == "unknown" and f["hours_today"] == "native"
    assert prof["scheduling"]["fields"]["length_ft"] == "native"
    w.apply(BY_ID["severe_fault_with_spare"])                       # a fresh world restores every column
    assert w.system.toolset.capability_profiles("agency-a").value["workforce"]["fields"]["licence_class"] == "native"


def test_write_adapter_blocks_a_known_violation_found_at_write_time():
    """The adapter re-check uses the same clearance module: an expired credential found at write time blocks."""
    w = IntegrationWorld()
    w.apply(BY_ID["severe_fault_with_spare"])
    ok = w.system.toolset.assign_operator("agency-a", "blk-1", "op-2", "k-ok")
    assert ok.status == "ok"
    w.mocks.clients["workforce"].post("/sim/operator", headers={"X-Agency": "agency-a"}, json={
        "operator": "OP-2", "training_valid_to": "P=2026-10-10T00:00:00Z;standard=2030-01-01T00:00:00Z;lift=2030-01-01T00:00:00Z"})
    r = w.system.toolset.assign_operator("agency-a", "blk-1", "op-2", "k-bad")
    assert r.is_blocked and r.blocked_reason == "hard_limit" and "expired" in r.note
