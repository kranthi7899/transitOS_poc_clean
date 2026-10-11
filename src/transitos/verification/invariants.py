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


# 1. Hard limits: an outcome tripwire on FINAL NUMBERS only ---------------------------------------
# Phase 10 (amendment A, Q2): the rules themselves (qualification, fit, hours, spares, double-booking) live in
# `transitos.clearance`, once. Verification keeps no second implementation; it only checks that the numbers
# the world ended up with are sane, and the golden table (tests/verification/golden_clearance.yaml, written
# from the policy text) checks the rules.
def _overlap(a, b) -> bool:
    return a.start < b.end and b.start < a.end


def hard_limits(obs: Observation) -> list[Violation]:
    """Tripwire: (1) no operator ended up over the daily/weekly hours cap, (2) the minimum number of spares is
    still left, (3) nothing a surviving write touched is double-booked."""
    name, out = "hard_limits", []
    blocks = {b.id: b for b in obs.blocks}
    operators = {o.id: o for o in obs.operators}
    lim = obs.policy.limits

    eff_assign = _effective(obs, "assign_operator")
    added: dict[tuple[str, str], float] = defaultdict(float)     # (run, operator) -> hours of blocks assigned
    for w in eff_assign:
        op, blk = operators.get(w.params.get("operator_id")), blocks.get(w.params.get("block_id"))
        if op is None or blk is None:
            out.append(_v(name, w.run_id, f"assign_operator references unknown operator/block: {w.params}"))
            continue
        added[(w.run_id, op.id)] += (blk.end - blk.start).total_seconds() / 3600
    for (rid, oid), hrs in added.items():
        op = operators[oid]
        unknown = set(op.unknown_fields)        # R2-4: a placeholder is not a number; see hard_limits_not_checked
        if "hours_today" not in unknown and op.hours_today + hrs > lim.max_operator_hours_day + 1e-9:
            out.append(_v(name, rid, f"{oid} {op.hours_today}h + {hrs}h > max {lim.max_operator_hours_day}h"))
        if "hours_week" not in unknown and op.hours_week + hrs > lim.max_operator_hours_week + 1e-9:
            out.append(_v(name, rid, f"{oid} {op.hours_week}h + {hrs}h > weekly max {lim.max_operator_hours_week}h"))

    reassigned = {w.run_id for w in _effective(obs, "reassign_block")}
    if reassigned:
        assigned = {b.vehicle_id for b in obs.blocks}
        faulty = {r.vehicle_id for r in obs.runs}
        spares_left = [v for v in obs.vehicles
                       if v.status == "ready" and v.id not in assigned and v.id not in faulty]
        if len(spares_left) < lim.min_spares:
            for rid in reassigned:
                out.append(_v(name, rid, f"{len(spares_left)} spare(s) left < min_spares {lim.min_spares}"))

    touched_v = {w.params.get("to_vehicle_id") for w in _effective(obs, "reassign_block")}
    touched_o = {w.params.get("operator_id") for w in eff_assign}
    for kind, ids_, key in (("vehicle", touched_v, lambda b: b.vehicle_id), ("operator", touched_o, lambda b: b.operator_id)):
        for rid in ids_ - {None}:
            mine = sorted((b for b in obs.blocks if key(b) == rid), key=lambda b: b.start)
            for i, x in enumerate(mine):
                for y in mine[i + 1:]:
                    if _overlap(x, y):
                        out.append(_v(name, None, f"{kind} {rid} double-booked on {x.id} and {y.id}"))
    return out


def hard_limits_not_checked(obs: Observation) -> list[str]:
    """What the hours tripwire SKIPPED (R2-4): an operator given a block whose hours are listed unknown carries
    only a placeholder, so that cap is reported as not checked rather than silently passed."""
    operators = {o.id: o for o in obs.operators}
    notes: list[str] = []
    for oid in sorted({w.params.get("operator_id") for w in _effective(obs, "assign_operator")} - {None}):
        op = operators.get(oid)
        for field, cap in (("hours_today", "daily"), ("hours_week", "weekly")):
            if op is not None and field in op.unknown_fields:
                notes.append(f"{oid}: {field} unknown, {cap} cap not checked")
    return notes


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


class Findings(list):
    """check_all's result: the violations (a plain list, compared and iterated exactly as before) plus
    `not_checked`, what the tripwire had to skip (R3-2). Notes, never violations."""

    def __init__(self, violations: list[Violation], not_checked: list[str]):
        super().__init__(violations)
        self.not_checked = not_checked


def check_all(obs: Observation) -> Findings:
    return Findings([v for fn in INVARIANTS.values() for v in fn(obs)], hard_limits_not_checked(obs))


def check_by_invariant(obs: Observation) -> dict[str, list[Violation]]:
    return {name: fn(obs) for name, fn in INVARIANTS.items()}
