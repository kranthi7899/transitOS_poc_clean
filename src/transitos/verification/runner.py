"""Interpret a Scenario against any World; classify the outcome; check expectations + invariants."""
from __future__ import annotations

from pydantic import BaseModel, Field

from .invariants import check_by_invariant
from .model import Observation, Violation
from .scenarios import Outcome, Scenario
from .world import World


def classify(obs: Observation) -> str:
    """Outcome class from contract-level fields only (RunState status/decision/violations/plan).

    Convention (assumption A11): violations are strings; those starting with `kill_switch` or
    `product_unavailable` mark those cases; any other violation means a hard limit blocked the plan.
    """
    if not obs.runs:
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


def drive(world: World, scenario: Scenario) -> Observation:
    world.apply(scenario)
    for op in scenario.script:
        name, arg = op[0], (op[1] if len(op) > 1 else None)
        runs = world.observe().runs
        rid = runs[0].run_id if runs else None
        if name == "deliver":
            world.deliver(scenario.events[arg])
        elif rid is None:
            continue
        elif name == "approve":
            world.approve(rid, scope=arg)
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
    obs = drive(world, scenario)
    outcome = classify(obs)
    mism: list[str] = []
    if outcome != scenario.expected.value:
        mism.append(f"outcome {outcome!r} != expected {scenario.expected.value!r}")
    if len(obs.runs) != scenario.expect_runs:
        mism.append(f"{len(obs.runs)} runs != expected {scenario.expect_runs}")
    if scenario.expect_wo_creates is not None and wo_creates(obs) != scenario.expect_wo_creates:
        mism.append(f"{wo_creates(obs)} WOs created != expected {scenario.expect_wo_creates}")
    if scenario.expect_plan_version is not None and obs.runs and obs.runs[0].plan_version != scenario.expect_plan_version:
        mism.append(f"plan_version {obs.runs[0].plan_version} != expected {scenario.expect_plan_version}")
    attached = sum(len(v) for v in obs.attached_events.values())
    if attached != scenario.expect_attached:
        mism.append(f"{attached} attached events != expected {scenario.expect_attached}")
    return ScenarioResult(scenario_id=scenario.id, title=scenario.title, expected=scenario.expected.value,
                          outcome=outcome, mismatches=mism, invariants=check_by_invariant(obs),
                          observation=obs if keep_observation else None)
