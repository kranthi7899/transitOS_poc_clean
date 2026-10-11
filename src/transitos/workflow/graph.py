"""LangGraph workflow (Track D). Depends only on contracts + injected interfaces.

gather -> check -> reason -> policy -> propose -> gate <-> (execute_step | await_approval -> route -> revalidate)
gate -> wait_for_repair -> route_repair -> [return_gate] -> return_to_service -> close
failure -> compensate (reverse, one undo per pass) -> escalate.

Invariants: nodes that call interrupt() (await_approval, wait_for_repair, return_gate) perform NO writes and
no store updates (LangGraph re-runs the node from its start on resume, A4). execute_step / compensate /
return_to_service perform exactly ONE product write per pass; state is checkpointed between passes.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import partial
from typing import Any, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from transitos.clearance.evaluate import (
    Assign,
    Option,
    noteworthy,
    operator_items,
    safe_evaluate,
    summarise,
    unacknowledged,
    vehicle_items,
)
from transitos.clearance.facts import CapabilityProfile, Facts, commitments_from_blocks
from transitos.clearance.rank import rank_plans
from transitos.clearance.rules import min_distinct_spares
from transitos.contracts.ids import compensation_key, step_key
from transitos.contracts.objects import (
    Block,
    DecisionRecord,
    Needs,
    Operator,
    Plan,
    RunState,
    Step,
    Vehicle,
)
from transitos.contracts.policy import AgencyPolicy
from transitos.contracts.results import Result
from transitos.contracts.tools import COMPENSATION
from transitos.workflow.limits import HardLimits
from transitos.workflow.plan import build_plan, step_name
from transitos.workflow.reasoning import (
    Assignment,
    ModelOutput,
    Reasoner,
    StubReasoner,
    code_ranked_fallback,
    validate_output,
)
from transitos.workflow.store import RunsIndex

RECURSION_LIMIT = 500


class WState(TypedDict):
    run: dict
    ctx: dict


@dataclass
class Deps:
    tools: Any                                   # ToolSet-like: .reads/.writes attrs, or one object with both
    policy: Callable[[], AgencyPolicy]           # called live so a kill-switch flip is seen immediately
    limits: HardLimits
    store: RunsIndex
    clock: Callable[[], datetime]
    reasoner: Reasoner = field(default_factory=StubReasoner)
    horizon_hours: int = 24
    max_write_attempts: int = 3
    max_replans: int = 3

    @property
    def reads(self):
        return getattr(self.tools, "reads", self.tools)

    @property
    def writes(self):
        return getattr(self.tools, "writes", self.tools)


def run_config(run_id: str) -> dict:
    return {"configurable": {"thread_id": run_id}, "recursion_limit": RECURSION_LIMIT}


def make_checkpointer(path: str) -> SqliteSaver:
    return SqliteSaver(sqlite3.connect(path, check_same_thread=False))


def initial_state(run: RunState) -> dict:
    return {"run": run.model_dump(mode="json"), "ctx": {}}


def _load(state: WState) -> tuple[RunState, dict]:
    return RunState.model_validate(state["run"]), dict(state["ctx"])


def _out(run: RunState, ctx: dict, nxt: str | None = None) -> dict:
    if nxt:
        ctx["next"] = nxt
    return {"run": run.model_dump(mode="json"), "ctx": ctx}


def _parse(s: str | datetime) -> datetime:
    return s if isinstance(s, datetime) else datetime.fromisoformat(s)


def build_graph(deps: Deps, checkpointer):
    # ------------------------------------------------------------------ helpers
    def status(run: RunState, st: str) -> None:
        run.status = st
        deps.store.set_status(run.run_id, st)

    def record(run: RunState, ctx: dict, action: dict | None = None, **patch) -> None:
        d = deps.store.get_decision(run.run_id, run.plan_version) or DecisionRecord(
            run_id=run.run_id, plan_version=run.plan_version,
            trigger_event_id=run.trigger_event.get("id", ""),
            model=ctx.get("model", "stub"), prompt_version=ctx.get("prompt_version", "v0"),
        ).model_dump(mode="json")
        if action:
            key = action["key"]
            d["actions"] = [a for a in d["actions"] if a.get("key") != key] + [action]
        d.update(patch)
        deps.store.save_decision(run.run_id, run.plan_version, d)
        ref = f"decision:{run.run_id}:{run.plan_version}"
        if ref not in run.audit_refs:
            run.audit_refs.append(ref)

    def do_gather(run: RunState) -> tuple[dict | None, str | None]:
        now = deps.clock()
        end = now + timedelta(hours=deps.horizon_hours)
        r, ag, vid = deps.reads, run.agency, run.vehicle_id
        with ThreadPoolExecutor(max_workers=4) as ex:
            f_v = ex.submit(r.get_vehicle, ag, vid)
            f_wo = ex.submit(r.find_open_work_order, ag, vid)
            f_b = ex.submit(r.list_affected_blocks, ag, vid, now, end)
            f_r = ex.submit(r.list_ready_vehicles, ag, now, end, Needs())
            res = {"vehicle": f_v.result(), "work_order": f_wo.result(), "blocks": f_b.result(),
                   "ready": f_r.result()}
            for name, x in res.items():
                if x.status != "ok":
                    return None, f"unavailable:{name}:{x.product or ''}:{x.note or x.status}"
            blocks: list[Block] = res["blocks"].value or []
            per = list(ex.map(lambda b: (r.list_ready_vehicles(ag, b.start, b.end, b.needs),
                                         r.list_eligible_operators(ag, b)), blocks))
        items = []
        for b, (sp, ops) in zip(blocks, per):
            for name, x in (("spares", sp), ("operators", ops)):
                if x.status != "ok":
                    return None, f"unavailable:{name}:{x.product or ''}:{x.note or x.status}"
            items.append({"block": b.model_dump(mode="json"),
                          "spares": [v.model_dump(mode="json") for v in sp.value or []],
                          "operators": [o.model_dump(mode="json") for o in ops.value or []]})
        profiles: dict = {}
        if (prof_fn := getattr(r, "capability_profiles", None)) is not None:
            pr = prof_fn(ag)
            if pr.status != "ok":
                return None, f"unavailable:profiles:{pr.product or ''}:{pr.note or pr.status}"
            profiles = pr.value or {}
        schedule = None   # None = no schedule feed: double-booking then fails closed (clearance)
        if (sched_fn := getattr(r, "list_blocks", None)) is not None:
            sr = sched_fn(ag, now, end)
            if sr.status != "ok":
                return None, f"unavailable:schedule:{sr.product or ''}:{sr.note or sr.status}"
            schedule = None if sr.value is None else [b.model_dump(mode="json") for b in sr.value]
        wo = res["work_order"].value
        facts = {"now": now.isoformat(), "window_end": end.isoformat(), "profiles": profiles, "schedule": schedule,
                 "vehicle": res["vehicle"].value.model_dump(mode="json"),
                 "existing_wo": wo.model_dump(mode="json") if wo else None,
                 "ready_ids": sorted(v.id for v in res["ready"].value or [] if v.id != vid and v.status == "ready"),
                 "blocks": items}
        return facts, None

    def do_check(run: RunState, facts: dict) -> tuple[dict, list[str]]:
        """Per-block candidate lists through the clearance module: a candidate is dropped only for a KNOWN
        violation (unverifiable data keeps it listed; the plan ranks and flags it). The clearance snapshot
        the reasoner/validator/ranker need travels inside the model-facing dict."""
        lim, pol = deps.limits, deps.policy()
        profiles = {k: CapabilityProfile.from_dict(v) for k, v in (facts.get("profiles") or {}).items()}
        sched = facts.get("schedule")
        commitments = None if sched is None else commitments_from_blocks(Block.model_validate(x) for x in sched)
        ready_count = len(facts["ready_ids"])
        probe = Facts(blocks={}, vehicles={}, operators={}, ready_count=ready_count, commitments=commitments,
                      profiles=profiles, trigger_vehicle_id=run.vehicle_id)
        violations: list[str] = []
        blocks, block_objs, spare_ids = [], {}, {}
        vehicles: dict[str, Vehicle] = {}
        operators: dict[str, Operator] = {}
        for it in facts["blocks"]:
            b = Block.model_validate(it["block"])
            spares = [v for v in (Vehicle.model_validate(x) for x in it["spares"]) if v.id != run.vehicle_id
                      and not any(i.status == "violation" for i in vehicle_items(b, v, probe, pol))]
            ops = [o for o in (Operator.model_validate(x) for x in it["operators"])
                   if not any(i.status == "violation" for i in operator_items(b, o, b.needs.type, probe, pol))]
            needs_op = b.operator_id is None or b.operator_id not in {o.id for o in ops}
            if not spares:
                violations.append(f"no_feasible_spare:{b.id}")
            if needs_op and not ops:
                violations.append(f"no_feasible_operator:{b.id}")
            block_objs[b.id], spare_ids[b.id] = b, [v.id for v in spares]
            vehicles.update({v.id: v for v in spares})
            operators.update({o.id: o for o in ops})
            blocks.append({"block_id": b.id, "vehicle_id": b.vehicle_id, "start": b.start.isoformat(),
                           "end": b.end.isoformat(), "start_depot": b.start_depot, "needs": b.needs.model_dump(),
                           "current_operator": b.operator_id, "needs_operator": needs_op,
                           "spares": [v.model_dump(mode="json") for v in spares],
                           "operators": [o.model_dump(mode="json") for o in ops]})
        if blocks:   # a spare may serve several non-overlapping blocks: count the distinct spares needed
            need = min_distinct_spares(list(block_objs.values()), spare_ids)
            if need is not None and not lim.spares_left_ok(ready_count, need):
                violations.append(f"last_spare:{ready_count}_ready_for_{need}_distinct_spares")
        snapshot = Facts(blocks=block_objs, vehicles=vehicles, operators=operators, ready_count=ready_count,
                         commitments=commitments, profiles=profiles, trigger_vehicle_id=run.vehicle_id)
        mf = {"run_id": run.run_id, "trigger_type": run.trigger_event.get("type"), "now": facts["now"],
              "window_end": facts["window_end"], "vehicle": facts["vehicle"], "min_spares": lim.min_spares,
              "ready_count": ready_count, "blocks": blocks, "previous_errors": [],
              "clearance": snapshot.to_dict(), "clearance_policy": pol.model_dump(mode="json")}
        return mf, violations

    def plan_as_output(plan: Plan) -> ModelOutput:
        a: dict[str, Assignment] = {}
        for s in plan.steps:
            bid = s.params.get("block_id")
            if s.action == "reassign_block":
                a.setdefault(bid, Assignment(block_id=bid)).spare_id = s.params["to_vehicle_id"]
            elif s.action == "assign_operator":
                a.setdefault(bid, Assignment(block_id=bid)).operator_id = s.params["operator_id"]
        return ModelOutput(decision=plan.decision, assignments=list(a.values()),
                           pull_at=plan.pull_at if plan.decision == "pull_at_block_end" else None)

    def make_plan(run: RunState, ctx: dict, out: ModelOutput) -> Plan:
        facts = ctx["facts"]
        return build_plan(run.run_id, run.plan_version, out, run.vehicle_id, facts["existing_wo"],
                          ctx["mf"]["blocks"], _parse(facts["now"]), run.trigger_event.get("type", ""))

    def option_of(out: ModelOutput, mf: dict) -> Option:
        cur = {b["block_id"]: b.get("current_operator") for b in mf["blocks"]}
        return Option(tuple(Assign(a.block_id, a.spare_id or "", None if a.operator_id in (None, cur.get(a.block_id))
                                   else a.operator_id) for a in out.assignments))

    def finish_plan(run: RunState, ctx: dict, out: ModelOutput, *, rank: bool) -> Plan:
        """Build the plan; when `rank`, choose among the model's proposal and code-generated options by
        clearance (hard rules decide the list, then tier, unverifiable, warnings, soft score; top three kept),
        otherwise just attach the clearance of this exact option (human edits)."""
        mf = ctx["mf"]
        if out.decision == "monitor":
            return make_plan(run, ctx, out)
        cf, pol, at = Facts.from_dict(mf["clearance"]), deps.policy(), _parse(mf["now"])
        seed = option_of(out, mf)
        alternatives: list[dict] = []
        reason, chosen = "", None
        if rank:
            pull_at = out.pull_at if (out.decision == "pull_at_block_end" and out.pull_at) else at
            req = [b for b in mf["blocks"] if _parse(b["end"]) > pull_at]
            ranked = rank_plans([seed], [cf.blocks[b["block_id"]] for b in req],
                                {b["block_id"]: [x["id"] for x in b["spares"]] for b in req},
                                {b["block_id"]: [x["id"] for x in b["operators"]] for b in req}, cf, pol, at)
            if ranked.options:
                chosen, reason = ranked.options[0], ranked.reason_first_beat_second
                alternatives = [{"rank": r.rank, "tier": r.clearance.tier, "warnings": r.clearance.warnings,
                                 "unverifiable": r.clearance.unverifiable,
                                 "assignments": [{"block_id": a.block_id, "spare_id": a.spare_id,
                                                  "operator_id": a.operator_id} for a in r.option.assignments],
                                 "clearance": [i.model_dump() for i in summarise(r.clearance.items)
                                               if i.requires_ack or i.status == "warn"]}
                                for r in ranked.top3[1:]]
        if chosen is not None:
            out = out.model_copy(update={"assignments": [Assignment(block_id=a.block_id, spare_id=a.spare_id,
                                                                    operator_id=a.operator_id)
                                                         for a in chosen.option.assignments]})
            c = chosen.clearance
        else:
            c = safe_evaluate(seed, cf, pol, at)
        plan = make_plan(run, ctx, out)
        plan.clearance = summarise(c.items)
        plan.vehicle_fit = c.tier
        plan.rank_reason = reason
        plan.alternatives = list(plan.alternatives) + alternatives
        return plan

    def call_write(fn, *args) -> Result:
        res: Result = Result.unavailable("tools", "no attempt")
        for _ in range(deps.max_write_attempts):
            try:
                res = fn(*args)
            except Exception as e:  # noqa: BLE001  (BaseException, e.g. process kill, propagates)
                res = Result.unavailable("tools", f"exception:{type(e).__name__}:{e}")
            if res.status == "ok":
                return res
            if res.status == "partial":  # write sent, read-back failed: reconcile, never blindly retry
                return res
            if res.is_blocked:  # kill switch / hard limit block: retrying is pointless
                return res
        return res

    def write_for(run: RunState, step: Step) -> Result:
        w, a, p, k = deps.writes, run.agency, step.params, step.idempotency_key
        match step.action:
            case "create_work_order":
                return call_write(w.create_work_order, a, p["vehicle_id"], p["description"], k)
            case "set_vehicle_unavailable":
                end = _parse(p["end"]) if p.get("end") else None
                return call_write(w.set_vehicle_unavailable, a, p["vehicle_id"], _parse(p["start"]), end, k)
            case "reassign_block":
                return call_write(w.reassign_block, a, p["block_id"], p["to_vehicle_id"], k)
            case "assign_operator":
                return call_write(w.assign_operator, a, p["block_id"], p["operator_id"], k)
            case "publish_rider_alert":
                return call_write(w.publish_rider_alert, a, p["block_id"], p["message"], k)
            case "set_vehicle_available":
                return call_write(w.set_vehicle_available, a, p["vehicle_id"], k)
        return Result.unavailable("workflow", f"unknown action {step.action}")

    def compensation_for(run: RunState, step: Step) -> Result:
        w, a, p = deps.writes, run.agency, step.params
        k = compensation_key(run.run_id, step_name(step.action, p), run.plan_version)
        match step.action:
            case "create_work_order":
                return call_write(w.cancel_work_order, a, p.get("result_id", ""), k)
            case "set_vehicle_unavailable":
                return call_write(w.set_vehicle_available, a, p["vehicle_id"], k)
            case "reassign_block":
                return call_write(partial(w.reassign_block, compensation=True), a, p["block_id"], p["from_vehicle_id"], k)
            case "assign_operator":
                if not p.get("from_operator_id"):
                    return Result.unavailable("workflow", "no prior operator to restore")
                return call_write(partial(w.assign_operator, compensation=True), a, p["block_id"],
                                  p["from_operator_id"], k)
        return Result.unavailable("workflow", f"no compensation for {step.action}")

    # ------------------------------------------------------------------ nodes
    def pm_not_urgent(run: RunState) -> str | None:
        """Policy rule in code (A7): PM overdue below the urgent threshold -> monitor, no plan, no writes."""
        trig = run.trigger_event
        if trig.get("type") != "PMOverdue":
            return None
        d = trig.get("data") or {}
        over = int(d.get("meter_mi", 0)) - int(d.get("due_mi", 0))
        thr = deps.policy().limits.pm_urgent_overdue_mi
        return f"PM {over} mi overdue < urgent threshold {thr} mi: monitor" if over < thr else None

    def gather(state: WState):
        run, ctx = _load(state)
        status(run, "GATHERING")
        if (why := pm_not_urgent(run)) is not None:
            run.plan = Plan(decision="monitor", reasoning=why, assumptions=["policy rule (code), no model call"])
            ctx["model"], ctx["prompt_version"] = "policy_rule", "n/a"
            return _out(run, ctx, "policy")
        facts, err = do_gather(run)
        if err:
            ctx["reason"] = err
            return _out(run, ctx, "escalate")
        ctx["facts"] = facts
        return _out(run, ctx, "check")

    def check(state: WState):
        run, ctx = _load(state)
        mf, viol = do_check(run, ctx["facts"])
        ctx["mf"] = mf
        run.violations = viol
        run.facts = {"vehicle_status": ctx["facts"]["vehicle"]["status"], "blocks": [b["block_id"] for b in mf["blocks"]],
                     "ready_spares": ctx["facts"]["ready_ids"], "existing_wo": (ctx["facts"]["existing_wo"] or {}).get("id")}
        if viol:
            ctx["reason"] = "hard_limit_violation"
            return _out(run, ctx, "escalate")
        return _out(run, ctx, "reason")

    def reason(state: WState):
        run, ctx = _load(state)
        mf = dict(ctx["mf"])
        parsed, errs, used_fallback = None, [], False
        for attempt in range(2):  # one retry
            mf["previous_errors"] = errs
            try:
                raw = deps.reasoner.propose(mf, attempt)
            except Exception as e:  # noqa: BLE001
                errs = [f"reasoner_error:{type(e).__name__}"]
                continue
            parsed, errs = validate_output(raw, mf)
            if not errs:
                break
        first_errors = errs
        if errs:
            used_fallback = True
            parsed, errs = validate_output(code_ranked_fallback(mf), mf)
            if errs:
                ctx["reason"] = "no_valid_plan:" + ",".join(errs)
                run.violations = [e for e in errs if e.startswith("clearance:")] or list(errs)
                return _out(run, ctx, "escalate")
        assert parsed is not None
        plan = finish_plan(run, ctx, parsed, rank=True)
        plan.fallback_used = used_fallback
        if used_fallback:
            plan.assumptions.append("model output rejected: " + "; ".join(first_errors))
        ctx["model"] = "code_ranked_fallback" if used_fallback else deps.reasoner.name
        ctx["prompt_version"] = deps.reasoner.prompt_version
        run.plan = plan
        run.decision, run.decided_by, run.decided_at = None, None, None
        ctx["approved_version"] = None
        existing = ctx["facts"]["existing_wo"]
        run.wo_id = existing["id"] if existing else run.wo_id  # human/other-opened WO is reused
        return _out(run, ctx, "policy")

    def policy_node(state: WState):
        run, ctx = _load(state)
        pol = deps.policy()
        assert run.plan is not None
        if run.plan.decision == "monitor":
            return _out(run, ctx, "propose")
        if pol.kill_switch:
            ctx["recommend_only"] = "kill_switch"
        elif any(pol.autonomy.get(s.action, "approve") == "recommend" for s in run.plan.steps):
            ctx["recommend_only"] = "recommend_only_autonomy"
        else:
            ctx.pop("recommend_only", None)
        return _out(run, ctx, "propose")

    def propose(state: WState):
        run, ctx = _load(state)
        assert run.plan is not None
        status(run, "PROPOSED")
        deps.store.save_plan(run.run_id, run.plan_version, run.plan.model_dump(mode="json"))
        record(run, ctx, facts_digest={"vehicle": run.vehicle_id, "blocks": run.facts.get("blocks", []),
                                       "ready_spares": run.facts.get("ready_spares", []),
                                       "violations": run.violations},
               recommendation={"decision": run.plan.decision, "pull_at": run.plan.pull_at,
                               "steps": [s.action for s in run.plan.steps],
                               "fallback_used": run.plan.fallback_used, "reasoning": run.plan.reasoning},
               model=ctx.get("model", "stub"), prompt_version=ctx.get("prompt_version", "v0"),
               clearance=[i.model_dump() for i in run.plan.clearance if noteworthy(i)],
               vehicle_fit=deps.policy().vehicle_fit)
        if run.plan.decision == "monitor":
            ctx["reason"] = "monitor"
            return _out(run, ctx, "close")
        if ctx.get("recommend_only"):
            ctx["reason"] = ctx["recommend_only"]
            return _out(run, ctx, "escalate")
        return _out(run, ctx, "gate")

    def gate(state: WState):
        """Decides what happens next. Does the status/deadline bookkeeping so the interrupt nodes stay pure."""
        run, ctx = _load(state)
        assert run.plan is not None
        pol = deps.policy()
        pending = [s for s in run.plan.steps if s.status == "pending"]
        if not pending:
            status(run, "WAITING_FOR_REPAIR")
            early = deps.store.pop_pending_resume(run.run_id)
            if early is not None:
                ctx["repair"] = early
                return _out(run, ctx, "route_repair")
            return _out(run, ctx, "wait_for_repair")
        auto = pol.autonomy.get(pending[0].action, "approve")
        missing = unacknowledged(run.plan.clearance, run.acknowledged)
        if missing and ctx.get("approved_version") == run.plan_version:
            ctx["approved_version"] = None   # approved without acknowledging everything: ask again
            run.decision, run.decided_by, run.decided_at = None, None, None
            ctx["notice"] = "acknowledgement required for: " + ", ".join(missing)
        if (auto == "approve" or missing) and ctx.get("approved_version") != run.plan_version:
            status(run, "AWAITING_APPROVAL")
            now = deps.clock()
            deps.store.set_awaiting(run.run_id, now, now + timedelta(minutes=pol.limits.approval_timeout_min))
            return _out(run, ctx, "await_approval")
        status(run, "EXECUTING")
        return _out(run, ctx, "execute_step")

    def await_approval(state: WState):
        # INTERRUPT ONLY: re-run from the top on resume; no writes, no store updates.
        run, ctx = state["run"], dict(state["ctx"])
        resp = interrupt({"kind": "approval", "run_id": run["run_id"], "plan_version": run["plan_version"],
                          "plan": run["plan"], "notice": ctx.get("notice")})
        ctx["resume"] = resp
        ctx["notice"] = None
        return {"ctx": ctx}

    def route(state: WState):
        run, ctx = _load(state)
        resp = ctx.pop("resume", None) or {}
        decision = resp.get("decision")
        if resp.get("stale"):
            ctx["stale"] = True
        if decision not in ("approve", "edit", "reject", "expired"):
            ctx["notice"] = f"invalid decision {decision!r}"
            return _out(run, ctx, "gate")
        if decision in ("approve", "edit"):
            run.acknowledged = sorted(set(run.acknowledged) | set(resp.get("acknowledged") or []))
        run.decision = decision
        run.decided_by = resp.get("by")
        run.decided_at = _parse(resp["at"]) if resp.get("at") else deps.clock()
        record(run, ctx, human_action={"decision": decision, "by": run.decided_by,
                                       "at": run.decided_at.isoformat(), "edits": resp.get("edits")},
               acknowledged=list(run.acknowledged))
        wrote = bool(run.steps_done)
        if decision == "reject":
            if wrote:
                ctx["reason"] = "rejected_after_writes"
                return _out(run, ctx, "compensate")
            return _out(run, ctx, "close_rejected")
        if decision == "expired":
            ctx["reason"] = "approval_expired"
            return _out(run, ctx, "compensate" if wrote else "escalate")
        if decision == "edit":
            if wrote:
                ctx["reason"] = "edit_not_supported_after_writes"
                return _out(run, ctx, "compensate")
            assert run.plan is not None
            base = plan_as_output(run.plan)
            merged = {a.block_id: a for a in base.assignments}
            for e in (resp.get("edits") or {}).get("assignments", []):
                cur = merged.get(e["block_id"], Assignment(block_id=e["block_id"]))
                merged[e["block_id"]] = cur.model_copy(update={k: v for k, v in e.items() if k != "block_id"})
            cand = base.model_copy(update={"assignments": list(merged.values())})
            _, errs = validate_output(cand.model_dump(mode="json"), ctx["mf"])
            if errs:
                ctx["notice"] = "edit rejected: " + ",".join(errs)
                run.decision = None
                return _out(run, ctx, "gate")
            run.plan_version += 1
            run.acknowledged = []   # A74 / F10: a new plan version clears acknowledgements (edits included)
            plan = finish_plan(run, ctx, cand.model_copy(update={"reasoning": run.plan.reasoning + " (edited by human)"}),
                               rank=False)
            plan.fallback_used = run.plan.fallback_used
            run.plan = plan
            deps.store.save_plan(run.run_id, run.plan_version, plan.model_dump(mode="json"))
            record(run, ctx, recommendation={"decision": plan.decision, "edited": True,
                                             "steps": [s.action for s in plan.steps]},
                   clearance=[i.model_dump() for i in plan.clearance if noteworthy(i)], acknowledged=[])
        return _out(run, ctx, "revalidate")

    def revalidate(state: WState):
        run, ctx = _load(state)
        assert run.plan is not None
        status(run, "REVALIDATING")
        stale = bool(ctx.pop("stale", False))
        facts, err = do_gather(run)
        if err:
            ctx["reason"] = err
            return _out(run, ctx, "compensate" if run.steps_done else "escalate")
        mf, viol = do_check(run, facts)
        ctx["facts"], ctx["mf"], run.violations = facts, mf, viol
        wrote = bool(run.steps_done)
        if not stale and not viol and not wrote:
            _, errs = validate_output(plan_as_output(run.plan).model_dump(mode="json"), mf)
            stale = bool(errs)
            if errs:
                ctx["stale_reasons"] = errs
        if viol:
            ctx["reason"] = "hard_limit_violation_on_revalidate"
            return _out(run, ctx, "compensate" if wrote else "escalate")
        if stale:
            if wrote:
                ctx["reason"] = "stale_after_writes"
                return _out(run, ctx, "compensate")
            ctx["replans"] = ctx.get("replans", 0) + 1
            if ctx["replans"] > deps.max_replans:
                ctx["reason"] = "too_many_replans"
                return _out(run, ctx, "escalate")
            run.plan_version += 1  # fresh idempotency keys, fresh approval; no write happened
            run.acknowledged = []
            return _out(run, ctx, "reason")
        ctx["approved_version"] = run.plan_version
        return _out(run, ctx, "gate")

    def execute_step(state: WState):
        run, ctx = _load(state)
        assert run.plan is not None
        if deps.policy().kill_switch:
            ctx["reason"] = "kill_switch"
            return _out(run, ctx, "escalate")
        if not run.steps_done and (missing := unacknowledged(run.plan.clearance, run.acknowledged)):
            ctx["reason"] = "unacknowledged:" + ",".join(missing)
            return _out(run, ctx, "escalate")
        step = next(s for s in run.plan.steps if s.status == "pending")
        name = step_name(step.action, step.params)
        res = write_for(run, step)
        at = deps.clock().isoformat()
        if res.status in ("ok", "partial"):
            step.status = "done"
            if res.status == "partial":
                ctx.setdefault("unverified", []).append(name)
            if step.action == "create_work_order" and res.value is not None:
                step.params["result_id"] = res.value.id
                run.wo_id = res.value.id
            run.steps_done.append(step.model_copy(deep=True))
            record(run, ctx, action={"key": step.idempotency_key, "kind": "write", "step": name,
                                     "status": "done", "at": at})
            return _out(run, ctx, "gate")
        step.status = "failed"
        record(run, ctx, action={"key": step.idempotency_key, "kind": "write", "step": name,
                                 "status": "failed", "note": res.note, "at": at})
        ctx["reason"] = f"step_failed:{name}:{res.note or res.status}"
        if COMPENSATION.get(step.action) is None:
            # Irreversible step (rider alert): a failed attempt may still have reached riders, and nothing but
            # return-to-service may follow an alert attempt. Keep the applied (safety) steps, escalate.
            ctx["reason"] = f"irreversible_step_failed:{name}:{res.note or res.status}"
            return _out(run, ctx, "escalate")
        status(run, "COMPENSATING")
        return _out(run, ctx, "compensate")

    def compensate(state: WState):
        run, ctx = _load(state)
        assert run.plan is not None
        status(run, "COMPENSATING")
        todo = [s for s in reversed(run.steps_done) if s.status == "done" and COMPENSATION.get(s.action)]
        if not todo:
            return _out(run, ctx, "escalate")
        if deps.policy().kill_switch:
            ctx["original_reason"] = ctx.get("reason")
            ctx["reason"] = "kill_switch_during_compensation"
            return _out(run, ctx, "escalate")
        s = todo[0]
        res = compensation_for(run, s)
        name = step_name(s.action, s.params)
        comp_key = compensation_key(run.run_id, name, run.plan_version)
        at = deps.clock().isoformat()
        if res.status in ("ok", "partial"):
            for lst in (run.steps_done, run.plan.steps):
                for x in lst:
                    if x.idempotency_key == s.idempotency_key:
                        x.status = "compensated"
            run.compensations_done.append(s.model_copy(update={"status": "compensated"}, deep=True))
            record(run, ctx, action={"key": comp_key, "compensates": s.idempotency_key, "kind": "compensation",
                                     "step": name, "status": "compensated", "at": at})
            return _out(run, ctx, "compensate")
        ctx["original_reason"] = ctx.get("reason")
        ctx["reason"] = "compensation_failed"
        ctx["comp_failed"] = {"step": name, "note": res.note, "key": s.idempotency_key}
        record(run, ctx, action={"key": comp_key, "compensates": s.idempotency_key, "kind": "compensation",
                                 "step": name, "status": "failed", "note": res.note, "at": at})
        return _out(run, ctx, "escalate")

    def escalate(state: WState):
        run, ctx = _load(state)
        # Contract-level outcome tags on RunState.violations (Track F's A11 classification convention).
        reason = str(ctx.get("reason") or "")
        tag = ("product_unavailable:" + reason.removeprefix("unavailable:") if reason.startswith("unavailable:")
               else "kill_switch" if reason.startswith("kill_switch") else None)
        if tag and tag not in run.violations:
            run.violations.append(tag)
        detail = {
            "reason": ctx.get("reason"), "original_reason": ctx.get("original_reason"),
            "violations": run.violations, "stale_reasons": ctx.get("stale_reasons"),
            "comp_failed": ctx.get("comp_failed"), "plan_version": run.plan_version, "wo_id": run.wo_id,
            "steps_done": [s.model_dump(mode="json") for s in run.steps_done],
            "compensations_done": [s.model_dump(mode="json") for s in run.compensations_done],
            "still_applied": [step_name(s.action, s.params) for s in run.steps_done if s.status == "done"],
            "plan_steps": [{"step": step_name(s.action, s.params), "status": s.status} for s in (run.plan.steps if run.plan else [])],
        }
        deps.store.save_escalation(run.run_id, detail)
        clean = bool(run.compensations_done) and ctx.get("reason") != "compensation_failed" and not detail["still_applied"]
        record(run, ctx, outcome="compensated" if clean else "escalated")
        run.status = "ESCALATED"
        deps.store.release(run.run_id, "ESCALATED")
        ctx["escalation"] = detail
        return _out(run, ctx)

    def close_rejected(state: WState):
        run, ctx = _load(state)
        record(run, ctx, outcome="rejected")
        run.status = "CLOSED_REJECTED"
        deps.store.release(run.run_id, "CLOSED_REJECTED")
        return _out(run, ctx)

    def wait_for_repair(state: WState):
        # INTERRUPT ONLY (resumed by WorkOrderClosed via starter).
        run, ctx = state["run"], dict(state["ctx"])
        ctx["repair"] = interrupt({"kind": "wait_for_repair", "run_id": run["run_id"], "wo_id": run.get("wo_id")})
        return {"ctx": ctx}

    def route_repair(state: WState):
        run, ctx = _load(state)
        status(run, "RETURNING")
        auto = deps.policy().autonomy.get("return_to_service", "approve")
        if auto == "recommend":
            ctx["reason"] = "return_to_service_recommend_only"
            return _out(run, ctx, "escalate")
        ctx["return_resume"] = {"decision": "approve", "by": "policy"} if auto == "act" else None
        return _out(run, ctx, "return_to_service" if auto == "act" else "return_gate")

    def return_gate(state: WState):
        # INTERRUPT ONLY
        run, ctx = state["run"], dict(state["ctx"])
        ctx["return_resume"] = interrupt({"kind": "return_approval", "run_id": run["run_id"],
                                          "vehicle_id": run["vehicle_id"]})
        return {"ctx": ctx}

    def return_to_service(state: WState):
        run, ctx = _load(state)
        resp = ctx.get("return_resume") or {}
        prev = (deps.store.get_decision(run.run_id, run.plan_version) or {}).get("human_action") or {}
        record(run, ctx, human_action={**prev, "return_to_service": {"decision": resp.get("decision"),
                                                                     "by": resp.get("by")}})
        if resp.get("decision") != "approve":
            ctx["reason"] = "return_to_service_not_approved"
            return _out(run, ctx, "escalate")
        if deps.policy().kill_switch:
            ctx["reason"] = "kill_switch"
            return _out(run, ctx, "escalate")
        step = Step(action="set_vehicle_available", params={"vehicle_id": run.vehicle_id},
                    idempotency_key=step_key(run.run_id, "set_vehicle_available", run.plan_version))
        res = write_for(run, step)
        if res.status != "ok":
            ctx["reason"] = f"return_to_service_failed:{res.note or res.status}"
            return _out(run, ctx, "escalate")
        step.status = "done"
        run.steps_done.append(step)
        record(run, ctx, action={"key": step.idempotency_key, "kind": "write", "step": "set_vehicle_available",
                                 "status": "done", "at": deps.clock().isoformat()})
        return _out(run, ctx, "close")

    def close(state: WState):
        run, ctx = _load(state)
        record(run, ctx, outcome="done")
        run.status = "DONE"
        deps.store.release(run.run_id, "DONE")
        return _out(run, ctx)

    # ------------------------------------------------------------------ wiring
    g = StateGraph(WState)
    nodes = {"gather": gather, "check": check, "reason": reason, "policy": policy_node, "propose": propose,
             "gate": gate, "await_approval": await_approval, "route": route, "revalidate": revalidate,
             "execute_step": execute_step, "compensate": compensate, "escalate": escalate,
             "close_rejected": close_rejected, "wait_for_repair": wait_for_repair,
             "route_repair": route_repair, "return_gate": return_gate,
             "return_to_service": return_to_service, "close": close}
    for n, fn in nodes.items():
        g.add_node(n, fn)
    g.add_edge(START, "gather")
    targets = {
        "gather": ["check", "escalate", "policy"], "check": ["reason", "escalate"], "reason": ["policy", "escalate"],
        "policy": ["propose"], "propose": ["gate", "escalate", "close"],
        "gate": ["await_approval", "execute_step", "wait_for_repair", "route_repair"],
        "route": ["revalidate", "gate", "compensate", "escalate", "close_rejected"],
        "revalidate": ["gate", "reason", "compensate", "escalate"],
        "execute_step": ["gate", "compensate", "escalate"], "compensate": ["compensate", "escalate"],
        "route_repair": ["return_gate", "return_to_service", "escalate"],
        "return_to_service": ["close", "escalate"],
    }
    for n, ts in targets.items():
        g.add_conditional_edges(n, (lambda s: s["ctx"]["next"]), {t: t for t in ts})
    g.add_edge("await_approval", "route")
    g.add_edge("wait_for_repair", "route_repair")
    g.add_edge("return_gate", "return_to_service")
    for terminal in ("escalate", "close_rejected", "close"):
        g.add_edge(terminal, END)
    return g.compile(checkpointer=checkpointer)
