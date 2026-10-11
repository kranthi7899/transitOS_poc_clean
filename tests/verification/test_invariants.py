"""Each invariant: a passing case (real FakeWorld happy run) and a deliberately violating case."""
import pytest

from transitos.verification import invariants as inv
from transitos.verification.fake_world import FakeWorld
from transitos.verification.model import AuditEvent
from transitos.verification.runner import drive
from transitos.verification.scenarios import BY_ID


@pytest.fixture
def good():
    # the full five-step saga (+ return); the baseline scenario keeps the block's operator (4 steps)
    return drive(FakeWorld(), BY_ID["operator_swap_needed"])


def _write(obs, action):
    return next(w for w in obs.writes if w.action == action and not w.compensation)


def test_all_hold_on_good_run(good):
    assert inv.check_all(good) == []
    assert len(good.writes) == 6


# hard limits
def test_hard_limits_pass(good):
    assert inv.hard_limits(good) == []


def test_tripwire_daily_and_weekly_hours_on_final_numbers(good):
    op = next(o for o in good.operators if o.id == _write(good, "assign_operator").params["operator_id"])
    op.hours_today = 9
    assert any("max 10.0h" in v.detail for v in inv.hard_limits(good))
    op.hours_today, op.hours_week = 4, 68
    assert any("weekly max" in v.detail for v in inv.hard_limits(good))


def test_tripwire_does_not_re_implement_qualification_or_fit(good):
    """Phase 10 (amendment A, Q2): those rules live in clearance, verified by the golden table."""
    spare = next(v for v in good.vehicles if v.id == _write(good, "reassign_block").params["to_vehicle_id"])
    spare.wheelchair_positions, spare.lift_ok, spare.seats, spare.type = 0, False, 10, "van"
    op = next(o for o in good.operators if o.id == _write(good, "assign_operator").params["operator_id"])
    op.lift_trained, op.trained_vehicle_types, op.qualifications = False, [], []
    assert inv.hard_limits(good) == []


def test_tripwire_double_booking_of_a_touched_vehicle_or_operator(good):
    blk = good.blocks[0]
    clash = blk.model_copy(update={"id": "blk-x"})
    good.blocks.append(clash)                       # same vehicle, same operator, same time
    msgs = " | ".join(v.detail for v in inv.hard_limits(good))
    assert "vehicle" in msgs and "operator" in msgs and "double-booked" in msgs


def test_tripwire_ignores_double_booking_the_run_did_not_touch(good):
    other = good.blocks[0].model_copy(update={"id": "blk-y", "vehicle_id": "veh-77", "operator_id": "OP-77"})
    good.blocks += [other, other.model_copy(update={"id": "blk-z"})]      # pre-existing clash elsewhere
    assert inv.hard_limits(good) == []


def test_hard_limits_last_spare(good):
    for v in good.vehicles:
        if v.id not in ("veh-1", _write(good, "reassign_block").params["to_vehicle_id"]):
            v.status = "reserved"       # the other spare is gone => we used the last one
    assert any("min_spares" in v.detail for v in inv.hard_limits(good))


def test_tripwire_operator_hours_sum_over_several_blocks(good):
    w = _write(good, "assign_operator")
    good.blocks.append(good.blocks[0].model_copy(update={"id": "blk-9"}))
    good.writes.append(w.model_copy(update={"seq": 97, "key": "k9", "params": {**w.params, "block_id": "blk-9"}}))
    op = next(o for o in good.operators if o.id == w.params["operator_id"])
    op.hours_today = 3                                  # 3h + 4h + 4h = 11h > 10h
    assert any("8.0h" in v.detail and "max 10.0h" in v.detail for v in inv.hard_limits(good))


# idempotent writes
def test_idempotent_pass(good):
    assert inv.idempotent_writes(good) == []


def test_idempotent_same_key_retry_is_fine(good):
    w = _write(good, "create_work_order")
    good.writes.append(w.model_copy(update={"seq": 98}))
    assert inv.idempotent_writes(good) == []


def test_idempotent_two_work_orders_in_run(good):
    w = _write(good, "create_work_order")
    good.writes.append(w.model_copy(update={"seq": 98, "key": "other", "effect_id": "WO-2"}))
    assert any("work orders created" in v.detail for v in inv.idempotent_writes(good))


def test_idempotent_same_key_two_effects(good):
    w = _write(good, "create_work_order")
    good.writes.append(w.model_copy(update={"seq": 98, "effect_id": "WO-9"}))
    assert any("different effects" in v.detail for v in inv.idempotent_writes(good))


def test_idempotent_two_live_system_wos_on_vehicle(good):
    good.work_orders[0].status = "open"
    good.work_orders.append(good.work_orders[0].model_copy(update={"id": "WO-77"}))
    assert any("live system WOs" in v.detail for v in inv.idempotent_writes(good))


# rider alert last
def test_alert_last_pass(good):
    assert inv.rider_alert_last(good) == []


def test_alert_before_other_step(good):
    alert = _write(good, "publish_rider_alert")
    alert.seq = _write(good, "set_vehicle_unavailable").seq - 1
    assert any("after rider alert" in v.detail for v in inv.rider_alert_last(good))


def test_alert_after_failed_step(good):
    _write(good, "assign_operator").ok = False
    assert any("after failed" in v.detail for v in inv.rider_alert_last(good))


def test_alert_without_all_steps_done(good):
    good.writes = [w for w in good.writes if w.action != "assign_operator"]
    assert any("before step assign_operator" in v.detail for v in inv.rider_alert_last(good))


# audit
def test_audit_pass(good):
    assert inv.audit_complete(good) == []


def test_audit_missing_record(good):
    good.audit = [a for a in good.audit if not (a.kind == "write" and a.key == _write(good, "reassign_block").key)]
    assert [v.detail for v in inv.audit_complete(good)] == [
        f"write reassign_block key={_write(good, 'reassign_block').key} has no audit record"]


# approval before write
def test_approval_pass(good):
    assert inv.approval_before_write(good) == []


def test_write_without_any_approval(good):
    good.audit = [a for a in good.audit if a.kind != "approval"]
    assert len(inv.approval_before_write(good)) == 6


def test_write_before_approval(good):
    first = min(good.writes, key=lambda w: w.seq)
    plan_appr = next(a for a in good.audit if a.kind == "approval" and a.data["scope"] == "plan")
    first.seq = plan_appr.seq - 1
    assert len(inv.approval_before_write(good)) == 1


def test_approval_for_other_plan_version_does_not_cover(good):
    _write(good, "reassign_block").plan_version = 2
    assert len(inv.approval_before_write(good)) == 1


def test_autonomy_act_exempts_and_recommend_forbids(good):
    good.audit = [a for a in good.audit if a.kind != "approval"]
    good.policy.autonomy = {k: "act" for k in good.policy.autonomy}
    assert inv.approval_before_write(good) == []
    good.policy.autonomy = {k: "recommend" for k in good.policy.autonomy}
    assert len(inv.approval_before_write(good)) == 6


# kill switch
def test_kill_pass(good):
    assert inv.no_write_after_kill_switch(good) == []


def test_write_after_kill_switch(good):
    good.audit.append(AuditEvent(seq=_write(good, "reassign_block").seq - 0, kind="kill_switch", data={"on": True}))
    bad = inv.no_write_after_kill_switch(good)
    assert bad and all(v.invariant == "no_write_after_kill_switch" for v in bad)


def test_kill_switch_off_again_allows_writes(good):
    good.audit.append(AuditEvent(seq=0, kind="kill_switch", data={"on": True}))
    good.audit.append(AuditEvent(seq=1, kind="kill_switch", data={"on": False}))
    assert inv.no_write_after_kill_switch(good) == []


def test_kill_switch_policy_flag_without_history(good):
    good.policy.kill_switch = True
    assert len(inv.no_write_after_kill_switch(good)) == 6


def test_registry_covers_every_checker(good):
    assert set(inv.INVARIANTS) == {"hard_limits", "idempotent_writes", "rider_alert_last", "audit_complete",
                                   "approval_before_write", "no_write_after_kill_switch"}
    assert all(v == [] for v in inv.check_by_invariant(good).values())
