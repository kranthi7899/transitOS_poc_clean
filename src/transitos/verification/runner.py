"""Interpret a Scenario against any World; classify the outcome; check expectations + invariants."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from .invariants import check_by_invariant, hard_limits_not_checked
from .model import Observation, Violation
from .scenarios import Outcome, Scenario
from .world import World


def classify(obs: Observation) -> str:
    """Outcome class from contract-level fields only (RunState status/decision/violations/plan).

    Convention (assumption A11): violations are strings; those starting with `kill_switch` or
    `product_unavailable` mark those cases; any other violation means a hard limit blocked the plan.
    """
    if not obs.runs:
        if any(str(d.get("reason") or "").startswith("ambiguous") for d in obs.dead_letters):
            return Outcome.NEEDS_HUMAN_MATCH.value     # Phase 10 Step 2: parked for a human identity pick
        return Outcome.DEAD_LETTER.value if obs.dead_letters else "no-run"
    run = obs.runs[0]
    viol = run.violations
    if run.plan is not None and run.plan.decision == "monitor":
        return Outcome.MONITOR.value
    if run.status == "CLOSED_REJECTED":
        return Outcome.REJECTED.value
    if run.decision == "expired":
        return Outcome.EXPIRED_ESCALATED.value
    if any(v.startswith("kill_switch") for v in viol):
        return Outcome.HALTED_KILL_SWITCH.value
    if run.compensations_done:
        return Outcome.COMPENSATED_ESCALATED.value
    if any(v.startswith("product_unavailable") for v in viol):
        return Outcome.ESCALATE_DEGRADED.value
    if run.status == "ESCALATED":
        return Outcome.ESCALATE_NO_SAFE_OPTION.value
    if run.status in ("EXECUTING", "WAITING_FOR_REPAIR", "RETURNING", "DONE"):
        return Outcome.PLAN_VALID.value
    return f"in-progress:{run.status}"


def wo_creates(obs: Observation) -> int:
    return len({w.effect_id for w in obs.forward_writes() if w.action == "create_work_order" and w.ok})


class ScenarioResult(BaseModel):
    scenario_id: str
    title: str
    expected: str
    outcome: str
    mismatches: list[str] = Field(default_factory=list)
    invariants: dict[str, list[Violation]] = Field(default_factory=dict)
    not_checked: list[str] = Field(default_factory=list)   # R3-2: tripwire notes; never affect `passed`
    known_gap: str = ""                                # Step 3 fix round: a PASS that rests on a documented gap
    skipped: str = ""                                  # Step 3 fix round: why this world did not run the scenario
    latency: dict | None = None   # Phase 10 Step 3.4: only worlds with a world engine; never affects `passed`
    observation: Observation | None = None

    @property
    def outcome_ok(self) -> bool:
        return not self.mismatches

    @property
    def invariants_ok(self) -> bool:
        return not any(self.invariants.values())

    @property
    def passed(self) -> bool:
        return self.outcome_ok and self.invariants_ok


def drive(world: World, scenario: Scenario, notes: list[str] | None = None) -> Observation:
    world.apply(scenario)
    for op in scenario.script:
        name, arg = op[0], (op[1] if len(op) > 1 else None)
        runs = world.observe().runs
        rid = runs[0].run_id if runs else None
        if name == "expect_status":       # Phase 10 Step 4 (A114): the run's status at this point of the script
            got = runs[0].status if runs else "no run"
            if got != arg and notes is not None:
                notes.append(f"at {op} the run was {got!r}")
            continue
        if name == "deliver":
            world.deliver(scenario.events[arg])
        elif name == "world_until":      # Phase 10 Step 3: run the world engine to HH:MM on the scenario day
            world.advance_world_to(arg)
        elif name == "pick":                  # Phase 10 Step 2: a human picks the identity of a parked event
            world.pick(arg)
        elif rid is None:
            continue
        elif name == "approve":
            world.approve(rid, scope=arg)
        elif name == "ack":
            world.acknowledge(rid, list(arg))
        elif name == "reject":
            world.reject(rid)
        elif name == "advance":
            world.advance_clock(arg)
        elif name == "kill_switch":
            world.set_kill_switch(bool(arg))
        elif name == "close_wo":
            world.close_work_order(rid)
        elif name == "take_spare":
            world.take_spare(rid)
        else:
            raise ValueError(f"unknown script op {name!r}")
    return world.observe()


def run_scenario(world: World, scenario: Scenario, keep_observation: bool = False) -> ScenarioResult:
    if scenario.real_world_only and not getattr(world, "supports_world_lag", False):
        # a world that cannot model product lag does not get to agree with a scenario that depends on it
        return ScenarioResult(scenario_id=scenario.id, title=scenario.title, expected=scenario.expected.value,
                              outcome="SKIPPED", known_gap=scenario.known_gap,
                              skipped=scenario.real_world_reason
                              or "needs per-product lag, which this world does not model (FakeWorld has no lag)")
    mism: list[str] = []
    obs = drive(world, scenario, mism)
    outcome = classify(obs)
    if outcome != scenario.expected.value:
        mism.append(f"outcome {outcome!r} != expected {scenario.expected.value!r}")
    if len(obs.runs) != scenario.expect_runs:
        mism.append(f"{len(obs.runs)} runs != expected {scenario.expect_runs}")
    if scenario.expect_wo_creates is not None and wo_creates(obs) != scenario.expect_wo_creates:
        mism.append(f"{wo_creates(obs)} WOs created != expected {scenario.expect_wo_creates}")
    if scenario.expect_work_orders is not None and len(obs.work_orders) != scenario.expect_work_orders:
        mism.append(f"{len(obs.work_orders)} work orders in maintenance != expected {scenario.expect_work_orders}")
    if scenario.expect_pending_repairs is not None and len(obs.pending_repairs) != scenario.expect_pending_repairs:
        mism.append(f"{len(obs.pending_repairs)} pending repairs != expected {scenario.expect_pending_repairs}")
    if scenario.expect_shop_statuses is not None:   # F5: a subsequence of the statuses the foreman actually set
        done = [e["status"] for e in obs.shop_log if e.get("job") == "status" and e.get("skipped") is None]
        it = iter(done)
        if not all(s in it for s in scenario.expect_shop_statuses):
            mism.append(f"shop statuses {list(scenario.expect_shop_statuses)} not seen in order (saw {done})")
    for etype, at in scenario.expect_event_times or ():
        seen = sorted(e["time"] for e in obs.events if e["type"] == etype)
        if not any(datetime.fromisoformat(t) == datetime.fromisoformat(at) for t in seen):
            mism.append(f"no {etype} at {at} (saw {seen})")
    if scenario.expect_plan_version is not None and obs.runs and obs.runs[0].plan_version != scenario.expect_plan_version:
        mism.append(f"plan_version {obs.runs[0].plan_version} != expected {scenario.expect_plan_version}")
    if scenario.expect_forward_actions is not None:
        done = {w.action for w in obs.forward_writes() if w.ok}
        mism += [f"forward action {a!r} missing (did: {sorted(done)})" for a in scenario.expect_forward_actions
                 if a not in done]
    attached = sum(len(v) for v in obs.attached_events.values())
    if attached != scenario.expect_attached:
        mism.append(f"{attached} attached events != expected {scenario.expect_attached}")
    return ScenarioResult(scenario_id=scenario.id, title=scenario.title, expected=scenario.expected.value,
                          outcome=outcome, mismatches=mism, invariants=check_by_invariant(obs),
                          not_checked=hard_limits_not_checked(obs),
                          known_gap=scenario.known_gap, latency=world.latency() if hasattr(world, "latency") else None,
                          observation=obs if keep_observation else None)
