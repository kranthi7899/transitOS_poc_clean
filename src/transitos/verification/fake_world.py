"""FakeWorld: a small, synchronous, in-memory model of the intended Level 2 behaviour.

It exists so the invariants, scenarios and report can be exercised before the real tracks merge.
It is NOT the system under test and proves nothing about it; the real World (integration lead)
must pass the same tests.  Behaviour mirrors CLAUDE.md rules: hard limits in code, approval before
any write, saga order with rider alert last, compensation in reverse, kill switch before each write.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from transitos.clearance.evaluate import (
    operator_items,
    summarise,
    unacknowledged,
    vehicle_items,
)
from transitos.clearance.facts import Facts, commitments_from_blocks, native_profile
from transitos.clearance.rank import rank_plans
from transitos.clearance.rules import count_distinct_spares, guarded, last_spare_item
from transitos.contracts import ids
from transitos.contracts.envelope import START_TYPES, CloudEvent
from transitos.contracts.objects import (
    Block,
    Needs,
    Operator,
    Plan,
    RunState,
    Step,
    Vehicle,
    WorkOrder,
)
from transitos.contracts.policy import AgencyPolicy
from transitos.contracts.tools import COMPENSATION, SAGA_ORDER

from .model import RETURN_ACTION, AuditEvent, Observation, WriteRecord
from .scenarios import AGENCY, NATIVE_ASSET, NATIVE_TELEMATICS, T0, Scenario

BLOCK_HOURS = 4
LENGTH_FT = {"cutaway": 24.0, "standard": 40.0, "articulated": 60.0}   # ASSUMED A66
CROSSWALK = {NATIVE_TELEMATICS: "veh-1", NATIVE_ASSET: "veh-1", "BUS-44": "veh-1"}


def _step_comp(action: str) -> str | None:
    # Shim for coordination/requests/F-1.md: Step.compensation's Literal omits cancel_work_order.
    comp = COMPENSATION[action]
    return None if comp == "cancel_work_order" else comp


class FakeWorld:
    def __init__(self) -> None:
        self.apply(Scenario(id="_blank", title="", events=(), expected=None, script=()))  # type: ignore[arg-type]

    # ---- setup ----
    def apply(self, scenario: Scenario) -> None:
        t, self.chaos = scenario.tweaks, dict(scenario.chaos)
        self.tweaks = t
        self.seq = 0
        self.now = T0
        self.writes: list[WriteRecord] = []
        self.audit: list[AuditEvent] = []
        self.runs: dict[str, RunState] = {}
        self.proposed_at: dict[str, Any] = {}
        self.seen: set[str] = set()
        self.attached: dict[str, list[str]] = {}
        self.dead_letters: list[dict] = []
        self.alerts: list[dict] = []
        self.key_effects: dict[str, str] = {}
        self.wo_n = 0
        self.acks: dict[str, list[str]] = {}
        g = lambda name, default: getattr(t, name, default)
        self.policy = AgencyPolicy(agency=AGENCY, vehicle_fit=g("vehicle_fit", "allow_larger"))
        btype, stype = g("block_type", "standard"), g("spare_type", "standard")
        needs = Needs(seats=30, wheelchair_positions=t.block_wheelchair_positions, lift=t.block_needs_lift,
                      type=btype, max_vehicle_length_ft=g("block_max_length_ft", None))
        self.vehicles = {"veh-1": Vehicle(id="veh-1", agency=AGENCY, seats=40, wheelchair_positions=2, lift_ok=True,
                                          type=btype, status="in_service", depot="North", length_ft=LENGTH_FT[btype])}
        for i in range(t.spares):
            vid = f"veh-{i + 2}"
            self.vehicles[vid] = Vehicle(id=vid, agency=AGENCY, seats=t.spare_seats, status="ready", type=stype,
                                         wheelchair_positions=t.spare_wheelchair_positions, lift_ok=t.spare_lift_ok,
                                         depot="North", length_ft=g("spare_length_ft", None) or LENGTH_FT[stype])
        self.blocks = {"blk-1": Block(id="blk-1", vehicle_id="veh-1", operator_id="OP-1", start=T0 + timedelta(hours=1),
                                      end=T0 + timedelta(hours=1 + BLOCK_HOURS), start_depot="North", needs=needs)}
        if g("second_block", False):
            self.blocks["blk-2"] = Block(id="blk-2", vehicle_id="veh-1", operator_id="OP-2",
                                         start=T0 + timedelta(hours=g("second_block_start_h", 6.0)),
                                         end=T0 + timedelta(hours=g("second_block_start_h", 6.0) + 2),
                                         start_depot="North",
                                         needs=needs.model_copy(update={"seats": 20, "wheelchair_positions": 0,
                                                                        "lift": False}))
        trained = list(g("operator_trained_types", ("standard",))) if t.operators_qualified else ["van"]
        endorsements = list(g("operator_endorsements", ("P",)))
        offsets = dict(g("credential_days", ()))
        day = T0.replace(hour=0, minute=0)
        creds = [*endorsements, *trained, *(["lift"] if t.operators_lift_trained else [])]
        valid = {c: day + timedelta(days=offsets.get(c, 365)) for c in creds}
        hours = {"OP-1": t.operator_hours_today if t.assigned_operator_hours_today is None
                 else t.assigned_operator_hours_today, "OP-2": t.operator_hours_today}
        self.operators = {o: Operator(id=o, licence_class=g("operator_licence_class", "B"), endorsements=endorsements,
                                      trained_vehicle_types=trained, training_valid_to=valid,
                                      lift_trained=t.operators_lift_trained, hours_today=hours[o],
                                      hours_week=g("operator_hours_week", 20.0), on_extra_board=(o == "OP-2"),
                                      # F8: the medical card is a legal rule; the baseline carries a valid card
                                      # (as integration/fleets.py) so the 24 scenarios keep their meaning
                                      medical_valid_to=day + timedelta(days=365))
                          for o in ("OP-1", "OP-2")}
        self.profiles = {"scheduling": native_profile("scheduling"),
                         "workforce": native_profile("workforce", drop=tuple(g("drop_operator_fields", ())))}
        self.work_orders: dict[str, WorkOrder] = {}
        if t.human_wo_exists:
            self.work_orders["WO-H1"] = WorkOrder(id="WO-H1", native_no="H1", asset_vehicle_id="veh-1",
                                                  description="brake check", source="human", opened_at=T0)

    # ---- bookkeeping ----
    def _next(self) -> int:
        self.seq += 1
        return self.seq

    def _audit(self, kind: str, run_id: str | None = None, **kw: Any) -> None:
        self.audit.append(AuditEvent(seq=self._next(), kind=kind, run_id=run_id,  # type: ignore[arg-type]
                                     plan_version=kw.pop("plan_version", None), key=kw.pop("key", None), data=kw))

    # ---- event handling ----
    def deliver(self, event: CloudEvent) -> None:
        if event.id in self.seen:
            return
        self.seen.add(event.id)
        refs = event.data.get("native_vehicle_id") or event.data.get("native_asset_id")
        vid = CROSSWALK.get(refs)
        if vid is None:
            self.dead_letters.append({"event_id": event.id, "reason": "unresolved_vehicle_id", "native": refs})
            return
        if event.type not in START_TYPES:
            return
        active = [r for r in self.runs.values() if r.vehicle_id == vid and r.status not in ("DONE", "ESCALATED", "CLOSED_REJECTED")]
        if active:
            self.attached.setdefault(active[0].run_id, []).append(event.id)
            return
        run = RunState(run_id=ids.run_id(AGENCY, vid, event.id), agency=AGENCY, vehicle_id=vid,
                       trigger_event=event.model_dump(mode="json"), status="GATHERING")
        self.runs[run.run_id] = run
        self._gather_and_propose(run, event)

    def _escalate(self, run: RunState, *violations: str) -> None:
        run.status, run.violations = "ESCALATED", list(violations)
        self._audit("escalation", run.run_id, violations=list(violations))

    def _gather_and_propose(self, run: RunState, event: CloudEvent | None = None) -> None:
        if "scheduling" in self.chaos:
            return self._escalate(run, "product_unavailable:scheduling")
        if event is not None and event.type == "PMOverdue":
            over = event.data["meter_mi"] - event.data["due_mi"]
            if over < self.policy.limits.pm_urgent_overdue_mi:
                run.plan = Plan(decision="monitor", reasoning=f"PM only {over} mi overdue")
                run.status = "DONE"
                return
        affected = sorted((b for b in self.blocks.values() if b.vehicle_id == run.vehicle_id),
                          key=lambda b: (b.start, b.id))
        spares = [v for v in self.vehicles.values() if v.id != run.vehicle_id and v.status == "ready"
                  and not any(b.vehicle_id == v.id for b in self.blocks.values())]
        probe = Facts(blocks={}, vehicles={}, operators={}, ready_count=len(spares), profiles=self.profiles,
                      commitments=commitments_from_blocks(self.blocks.values()), trigger_vehicle_id=run.vehicle_id)
        viol: list[str] = []
        spare_ok: dict[str, list[str]] = {}
        op_ok: dict[str, list[str]] = {}
        def ok(where: str, fn, *args) -> bool:   # one candidate; an exception excludes only it (F4)
            return not any(i.status == "violation" for i in guarded(where, fn, *args))

        for b in affected:
            spare_ok[b.id] = [v.id for v in spares if ok(f"spare {v.id}", vehicle_items, b, v, probe, self.policy)]
            op_ok[b.id] = [o.id for o in self.operators.values()
                           if ok(f"operator {o.id}", operator_items, b, o, b.needs.type, probe, self.policy)]
            if not spare_ok[b.id]:
                viol.append(f"no_feasible_spare:{b.id}")
            if b.operator_id not in op_ok[b.id] and not op_ok[b.id]:
                viol.append(f"no_feasible_operator:{b.id}")
        need = count_distinct_spares(affected, spare_ok).n if affected else 0   # same function as the workflow
        if not viol and need is not None and \
                last_spare_item(len(spares), need, self.policy.limits.min_spares).status == "violation":
            viol.append("last_spare")
        ranked = None
        if not viol:
            snap = Facts(blocks={b.id: b for b in affected}, vehicles={v.id: v for v in spares},
                         operators=dict(self.operators), ready_count=len(spares), profiles=self.profiles,
                         commitments=commitments_from_blocks(self.blocks.values()), trigger_vehicle_id=run.vehicle_id)
            ranked = rank_plans([], affected, spare_ok, {b.id: op_ok[b.id] for b in affected}, snap, self.policy, self.now)
            if not ranked.options:
                viol.append("no_safe_option")
        if viol:
            return self._escalate(run, *viol)
        top = ranked.options[0]
        human_wo = next((w for w in self.work_orders.values() if w.source == "human" and w.status != "closed"), None)
        run.wo_id = human_wo.id if human_wo else None
        pv = run.plan_version
        raw: list[tuple[str, dict]] = []
        if not human_wo:
            raw.append(("create_work_order", {"vehicle_id": run.vehicle_id, "description": "fault"}))
        raw.append(("set_vehicle_unavailable", {"vehicle_id": run.vehicle_id}))
        for a in top.option.assignments:
            raw.append(("reassign_block", {"block_id": a.block_id, "to_vehicle_id": a.spare_id,
                                           "from_vehicle_id": run.vehicle_id}))
        for a in top.option.assignments:
            if a.operator_id:
                raw.append(("assign_operator", {"block_id": a.block_id, "operator_id": a.operator_id,
                                                "from_operator_id": self.blocks[a.block_id].operator_id}))
        for a in top.option.assignments:
            raw.append(("publish_rider_alert", {"block_id": a.block_id, "message": "Bus change"}))
        raw.sort(key=lambda t: SAGA_ORDER.index(t[0]))
        steps = [Step(action=act, params=p, idempotency_key=ids.step_key(
                      run.run_id, f"{act}:{p['block_id']}" if "block_id" in p else act, pv),
                      compensation=_step_comp(act))  # type: ignore[arg-type]
                 for act, p in raw]
        run.plan = Plan(decision="pull_now", steps=steps, reasoning="fake", clearance=summarise(top.clearance.items),
                        vehicle_fit=top.clearance.tier, rank_reason=ranked.reason_first_beat_second)
        run.status = "AWAITING_APPROVAL"
        self.proposed_at[run.run_id] = self.now
        self._audit("proposal", run.run_id, plan_version=pv)

    # ---- human + clock ----
    def approve(self, run_id: str, scope: str = "plan", by: str = "dispatcher") -> None:
        run = self.runs[run_id]
        if scope == "return_to_service":
            if run.status != "RETURNING":
                return
            self._audit("approval", run_id, plan_version=run.plan_version, scope=scope, by=by)
            self._write(run, RETURN_ACTION, {"vehicle_id": run.vehicle_id}, step_name=RETURN_ACTION)
            if run.status != "ESCALATED":
                run.status = "DONE"
            return
        if run.status != "AWAITING_APPROVAL":
            return
        self._audit("approval", run_id, plan_version=run.plan_version, scope="plan", by=by)
        run.acknowledged = sorted(set(run.acknowledged) | set(self.acks.get(run_id, [])))
        if run.plan is not None and unacknowledged(run.plan.clearance, run.acknowledged):
            return    # approved without acknowledging every requires_ack item: nothing executes
        run.decision, run.decided_by, run.decided_at = "approve", by, self.now
        spares = [st.params["to_vehicle_id"] for st in (run.plan.steps if run.plan else [])
                  if st.action == "reassign_block"]
        if not spares or any(self.vehicles[v].status != "ready" for v in spares):   # revalidate
            run.plan_version += 1
            run.plan, run.decision, run.acknowledged = None, None, []
            return self._gather_and_propose(run)
        run.status = "EXECUTING"
        self._execute(run)

    def reject(self, run_id: str, by: str = "dispatcher") -> None:
        run = self.runs[run_id]
        self._audit("rejection", run_id, by=by)
        run.decision, run.status = "reject", "CLOSED_REJECTED"

    def advance_clock(self, hours: float) -> None:
        self.now += timedelta(hours=hours)
        for run in self.runs.values():
            if run.status == "AWAITING_APPROVAL" and \
                    self.now - self.proposed_at[run.run_id] > timedelta(minutes=self.policy.limits.approval_timeout_min):
                run.decision = "expired"
                self._audit("expiry", run.run_id)
                self._escalate(run, "approval_expired")

    def set_kill_switch(self, on: bool) -> None:
        self.policy.kill_switch = on
        self._audit("kill_switch", None, on=on)

    def close_work_order(self, run_id: str) -> None:
        run = self.runs[run_id]
        if run.status == "WAITING_FOR_REPAIR":
            wo = self.work_orders[run.wo_id] if run.wo_id else None
            if wo:
                wo.status, wo.closed_at = "closed", self.now
            run.status = "RETURNING"

    def acknowledge(self, run_id: str, rules: list[str]) -> None:
        self.acks.setdefault(run_id, []).extend(rules)

    def take_spare(self, run_id: str) -> None:
        run = self.runs[run_id]
        spare = self.plan_spare(run)
        if spare:
            self.vehicles[spare].status = "reserved"

    @staticmethod
    def plan_spare(run: RunState) -> str | None:
        if not run.plan:
            return None
        return next((s.params["to_vehicle_id"] for s in run.plan.steps if s.action == "reassign_block"), None)

    # ---- execution ----
    def _execute(self, run: RunState) -> None:
        assert run.plan
        for step in run.plan.steps:
            if not self._write(run, step.action, step.params, step_name=step.action, key=step.idempotency_key):
                break
            step.status = "done"
            run.steps_done.append(step)
        else:
            run.status = "WAITING_FOR_REPAIR"
            return
        if run.status != "ESCALATED":      # a plain failure (not kill switch): compensate in reverse
            run.status = "COMPENSATING"
            for step in reversed(run.steps_done):
                comp = COMPENSATION.get(step.action)
                if comp is None:
                    continue
                self._write(run, comp, step.params, step_name=f"undo-{step.action}", compensation=True,
                            compensates=step.idempotency_key)
                step.status = "compensated"
                run.compensations_done.append(step)
            self._escalate(run, "write_failed")

    def _write(self, run: RunState, action: str, params: dict, *, step_name: str, key: str | None = None,
               compensation: bool = False, compensates: str | None = None) -> bool:
        pv = run.plan_version
        key = key or ids.step_key(run.run_id, step_name, pv)
        if self.policy.kill_switch:
            self._escalate(run, "kill_switch")
            return False
        n_done = len([w for w in self.writes if w.ok and not w.compensation and w.run_id == run.run_id])
        if not compensation and self.chaos.get("kill_after_writes") == n_done and n_done > 0:
            self.set_kill_switch(True)
            self._escalate(run, "kill_switch")
            return False
        calls = 2 if (self.chaos.get("maintenance") == "duplicate" and action == "create_work_order") else 1
        ok = self.chaos.get("reject_write") != action or compensation
        effect = None
        for _ in range(calls):           # a retried call with the same key must not repeat the effect
            if key in self.key_effects:
                effect = self.key_effects[key]
            elif ok:
                effect = self._apply(run, action, params, compensation)
                self.key_effects[key] = effect or ""
            self.writes.append(WriteRecord(seq=self._next(), run_id=run.run_id, action=action, key=key, plan_version=pv,
                                           params=params, ok=ok, effect_id=effect, compensation=compensation,
                                           compensates_key=compensates))
            self._audit("write", run.run_id, key=key, plan_version=pv, action=action, ok=ok)
        return ok

    def _apply(self, run: RunState, action: str, p: dict, compensation: bool) -> str | None:
        if action == "create_work_order":
            self.wo_n += 1
            wo = WorkOrder(id=f"WO-{self.wo_n}", native_no=str(self.wo_n), asset_vehicle_id=p["vehicle_id"],
                           description=p["description"], opened_at=self.now)
            self.work_orders[wo.id] = wo
            run.wo_id = wo.id
            return wo.id
        if action == "cancel_work_order":
            if run.wo_id:
                self.work_orders[run.wo_id].status = "closed"
            return run.wo_id
        if action == "set_vehicle_unavailable":
            self.vehicles[p["vehicle_id"]].status = "out_of_service"
        elif action == "set_vehicle_available":
            self.vehicles[p["vehicle_id"]].status = "ready"
        elif action == "reassign_block":
            self.blocks[p["block_id"]].vehicle_id = p["from_vehicle_id"] if compensation else p["to_vehicle_id"]
        elif action == "assign_operator":
            self.blocks[p["block_id"]].operator_id = p["from_operator_id"] if compensation else p["operator_id"]
        elif action == "publish_rider_alert":
            self.alerts.append({"block": p["block_id"], "message": p["message"]})
        return None

    # ---- observation ----
    def observe(self) -> Observation:
        return Observation(
            agency=AGENCY, policy=self.policy.model_copy(deep=True),
            runs=[r.model_copy(deep=True) for r in self.runs.values()],
            writes=list(self.writes), audit=list(self.audit),
            vehicles=[v.model_copy() for v in self.vehicles.values()],
            blocks=[b.model_copy(deep=True) for b in self.blocks.values()],
            operators=list(self.operators.values()), work_orders=list(self.work_orders.values()),
            alerts=list(self.alerts), dead_letters=list(self.dead_letters), attached_events=dict(self.attached))
