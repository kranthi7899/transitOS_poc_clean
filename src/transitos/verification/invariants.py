"""Pure invariant checkers over an `Observation` (final state + audit + write log).

Each checker returns a list of `Violation` (empty = holds).  They know nothing about how the
system under test is built.  Ordering is by the global `seq` shared by WriteRecord and AuditEvent.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable

from .model import RETURN_ACTION, Observation, Violation, WriteRecord

Checker = Callable[[Observation], list[Violation]]


def _v(name: str, run_id: str | None, detail: str) -> Violation:
    return Violation(invariant=name, run_id=run_id, detail=detail)


def _compensated_keys(obs: Observation) -> set[str]:
    return {w.compensates_key for w in obs.writes if w.compensation and w.ok and w.compensates_key}


def _effective(obs: Observation, action: str) -> list[WriteRecord]:
    """Successful forward writes of `action` that were not later undone."""
    undone = _compensated_keys(obs)
    return [w for w in obs.writes if w.action == action and w.ok and not w.compensation and w.key not in undone]


# 1. Hard limits -----------------------------------------------------------------------------------
def hard_limits(obs: Observation) -> list[Violation]:
    """Every surviving reassign/assign respects vehicle needs, min spares, operator hours and quals."""
    name, out = "hard_limits", []
    vehicles = {v.id: v for v in obs.vehicles}
    blocks = {b.id: b for b in obs.blocks}
    operators = {o.id: o for o in obs.operators}
    lim = obs.policy.limits

    reassigned_runs: dict[str, str] = {}
    for w in _effective(obs, "reassign_block"):
        veh, blk = vehicles.get(w.params.get("to_vehicle_id")), blocks.get(w.params.get("block_id"))
        if veh is None or blk is None:
            out.append(_v(name, w.run_id, f"reassign references unknown vehicle/block: {w.params}"))
            continue
        reassigned_runs[w.run_id] = veh.id
        n = blk.needs
        if veh.seats < n.seats:
            out.append(_v(name, w.run_id, f"{veh.id} seats {veh.seats} < needed {n.seats}"))
        if veh.wheelchair_positions < n.wheelchair_positions:
            out.append(_v(name, w.run_id, f"{veh.id} wheelchair positions {veh.wheelchair_positions} < needed {n.wheelchair_positions}"))
        if n.lift and not veh.lift_ok:
            out.append(_v(name, w.run_id, f"{veh.id} has no working lift but block {blk.id} needs one"))
        if n.type and veh.type != n.type:
            out.append(_v(name, w.run_id, f"{veh.id} type {veh.type} != needed {n.type}"))

    if reassigned_runs:
        assigned = {b.vehicle_id for b in obs.blocks}
        faulty = {r.vehicle_id for r in obs.runs}
        spares_left = [v for v in obs.vehicles
                       if v.status == "ready" and v.id not in assigned and v.id not in faulty]
        if len(spares_left) < lim.min_spares:
            for rid in reassigned_runs:
                out.append(_v(name, rid, f"{len(spares_left)} spare(s) left < min_spares {lim.min_spares}"))

    for w in _effective(obs, "assign_operator"):
        op, blk = operators.get(w.params.get("operator_id")), blocks.get(w.params.get("block_id"))
        if op is None or blk is None:
            out.append(_v(name, w.run_id, f"assign_operator references unknown operator/block: {w.params}"))
            continue
        hours = (blk.end - blk.start).total_seconds() / 3600
        if op.hours_today + hours > lim.max_operator_hours_day:
            out.append(_v(name, w.run_id, f"{op.id} {op.hours_today}h + {hours}h > max {lim.max_operator_hours_day}h"))
        if blk.needs.lift and not op.lift_trained:
            out.append(_v(name, w.run_id, f"{op.id} not lift-trained for lift block {blk.id}"))
        if blk.needs.type and blk.needs.type not in op.qualifications:
            out.append(_v(name, w.run_id, f"{op.id} lacks qualification {blk.needs.type}"))
    return out


# 2. Idempotent writes -----------------------------------------------------------------------------
def idempotent_writes(obs: Observation) -> list[Violation]:
    """<=1 work-order effect per run; one idempotency key never maps to two different effects."""
    name, out = "idempotent_writes", []
    effects_by_key: dict[str, set[str | None]] = defaultdict(set)
    wo_effects: dict[str, set[str | None]] = defaultdict(set)
    for w in obs.writes:
        if w.ok:
            effects_by_key[w.key].add(w.effect_id)
        if w.ok and not w.compensation and w.action == "create_work_order":
            wo_effects[w.run_id].add(w.effect_id)
    for key, effs in effects_by_key.items():
        if len(effs) > 1:
            out.append(_v(name, None, f"key {key} produced {len(effs)} different effects {sorted(map(str, effs))}"))
    for rid, effs in wo_effects.items():
        if len(effs) > 1:
            out.append(_v(name, rid, f"{len(effs)} work orders created in one run: {sorted(map(str, effs))}"))
    live_system_wos: dict[str, list[str]] = defaultdict(list)
    for wo in obs.work_orders:
        if wo.source == "system" and wo.status != "closed" and not _wo_cancelled(obs, wo.id):
            live_system_wos[wo.asset_vehicle_id].append(wo.id)
    for veh, wos in live_system_wos.items():
        if len(wos) > 1:
            out.append(_v(name, None, f"vehicle {veh} has {len(wos)} live system WOs: {wos}"))
    return out


def _wo_cancelled(obs: Observation, wo_id: str) -> bool:
    return any(w.ok and w.compensation and w.action == "cancel_work_order" and w.effect_id == wo_id for w in obs.writes)


# 3. Rider alert last ------------------------------------------------------------------------------
def rider_alert_last(obs: Observation) -> list[Violation]:
    """The (irreversible) rider alert is only published after every other plan step succeeded,
    with no compensation started, and nothing but the separate return-to-service follows it."""
    name, out = "rider_alert_last", []
    for run in obs.runs:
        mine = [w for w in obs.writes if w.run_id == run.run_id]
        for alert in (w for w in mine if w.action == "publish_rider_alert" and not w.compensation):
            others = [w for w in mine if w.action != "publish_rider_alert" and w.action != RETURN_ACTION
                      and w.plan_version == alert.plan_version]
            for w in others:
                if w.seq > alert.seq:
                    out.append(_v(name, run.run_id, f"{w.action} (seq {w.seq}) after rider alert (seq {alert.seq})"))
                elif not w.ok:
                    out.append(_v(name, run.run_id, f"rider alert published after failed {w.action}"))
            if run.plan is not None and run.plan.steps:
                done = {w.action for w in others if w.ok and not w.compensation and w.seq < alert.seq}
                for s in run.plan.steps:
                    if s.action not in ("publish_rider_alert", RETURN_ACTION) and s.action not in done:
                        out.append(_v(name, run.run_id, f"rider alert published before step {s.action} succeeded"))
    return out


# 4. Audit coverage --------------------------------------------------------------------------------
def audit_complete(obs: Observation) -> list[Violation]:
    """Every write (including failed ones and compensations) has an audit record with its key."""
    name = "audit_complete"
    audited = {(a.run_id, a.key) for a in obs.audit if a.kind == "write" and a.key}
    out = []
    for w in obs.writes:
        if (w.run_id, w.key) not in audited:
            out.append(_v(name, w.run_id, f"write {w.action} key={w.key} has no audit record"))
    return out


# 5. Approval before write -------------------------------------------------------------------------
def approval_before_write(obs: Observation) -> list[Violation]:
    """No forward write unless a human approval for that run (and plan version / scope) precedes it,
    unless the agency policy marks that action autonomy=act.  autonomy=recommend forbids all writes.
    Compensations are consequences of an approved plan and are exempt."""
    name, out = "approval_before_write", []
    approvals = [a for a in obs.audit if a.kind == "approval"]
    for w in obs.writes:
        if w.compensation:
            continue
        policy_key = "return_to_service" if w.action == RETURN_ACTION else w.action
        mode = obs.policy.autonomy.get(policy_key, "approve")
        if mode == "recommend":
            out.append(_v(name, w.run_id, f"{w.action} written under autonomy=recommend"))
            continue
        if mode == "act":
            continue
        scope = "return_to_service" if w.action == RETURN_ACTION else "plan"
        ok = any(a.run_id == w.run_id and a.seq < w.seq and a.data.get("scope", "plan") == scope
                 and (scope != "plan" or a.plan_version == w.plan_version) for a in approvals)
        if not ok:
            out.append(_v(name, w.run_id, f"{w.action} (seq {w.seq}, plan v{w.plan_version}) has no preceding {scope} approval"))
    return out


# 6. Kill switch -----------------------------------------------------------------------------------
def no_write_after_kill_switch(obs: Observation) -> list[Violation]:
    """No write at all (compensations included) while the kill switch is on."""
    name, out = "no_write_after_kill_switch", []
    toggles = sorted((a for a in obs.audit if a.kind == "kill_switch"), key=lambda a: a.seq)
    for w in obs.writes:
        on = obs.policy.kill_switch and not toggles   # policy says on, no history: on from the start
        for t in toggles:
            if t.seq < w.seq:
                on = bool(t.data.get("on"))
        if on:
            out.append(_v(name, w.run_id, f"{w.action} (seq {w.seq}) written while kill switch on"))
    return out


INVARIANTS: dict[str, Checker] = {
    "hard_limits": hard_limits,
    "idempotent_writes": idempotent_writes,
    "rider_alert_last": rider_alert_last,
    "audit_complete": audit_complete,
    "approval_before_write": approval_before_write,
    "no_write_after_kill_switch": no_write_after_kill_switch,
}


def check_all(obs: Observation) -> list[Violation]:
    return [v for fn in INVARIANTS.values() for v in fn(obs)]


def check_by_invariant(obs: Observation) -> dict[str, list[Violation]]:
    return {name: fn(obs) for name, fn in INVARIANTS.items()}
