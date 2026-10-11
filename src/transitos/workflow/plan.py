"""Deterministic plan construction from a VALIDATED model output (code builds steps, not the model)."""
from __future__ import annotations

from datetime import datetime

from transitos.contracts.ids import step_key
from transitos.contracts.objects import Plan, Step
from transitos.contracts.tools import COMPENSATION, SAGA_ORDER
from transitos.workflow.reasoning import ModelOutput


def step_name(action: str, params: dict) -> str:
    bid = params.get("block_id")
    return f"{action}:{bid}" if bid else action


def build_plan(run_id: str, plan_version: int, out: ModelOutput, vehicle_id: str,
               existing_wo: dict | None, blocks: list[dict], now: datetime, trigger_type: str) -> Plan:
    if out.decision == "monitor":
        return Plan(decision="monitor", reasoning=out.reasoning, alternatives=out.alternatives,
                    assumptions=out.assumptions)
    pull_at = out.pull_at if (out.decision == "pull_at_block_end" and out.pull_at) else now
    assign = {a.block_id: a for a in out.assignments}
    cur_op = {b["block_id"]: b.get("current_operator") for b in blocks}
    req = [b["block_id"] for b in blocks if b["block_id"] in assign]

    raw: list[tuple[str, dict]] = []
    if existing_wo is None:
        raw.append(("create_work_order", {"vehicle_id": vehicle_id,
                                          "description": f"Auto-created by TransitOS after {trigger_type}"}))
    raw.append(("set_vehicle_unavailable", {"vehicle_id": vehicle_id, "start": pull_at.isoformat(), "end": None}))
    for bid in req:
        raw.append(("reassign_block", {"block_id": bid, "to_vehicle_id": assign[bid].spare_id,
                                       "from_vehicle_id": vehicle_id}))
    for bid in req:
        new_op = assign[bid].operator_id
        if new_op and new_op != cur_op.get(bid):   # keep the block's operator unless the plan changes it
            raw.append(("assign_operator", {"block_id": bid, "operator_id": assign[bid].operator_id,
                                            "from_operator_id": cur_op.get(bid)}))
    for bid in req:
        raw.append(("publish_rider_alert", {"block_id": bid, "message":
                    f"Service change: block {bid} will run with a different vehicle."}))
    raw.sort(key=lambda t: SAGA_ORDER.index(t[0]))  # stable: saga order, then block order
    steps = [Step(action=a, params=p, idempotency_key=step_key(run_id, step_name(a, p), plan_version),
                  compensation=COMPENSATION[a]) for a, p in raw]  # type: ignore[arg-type]
    return Plan(decision=out.decision, pull_at=pull_at, steps=steps, reasoning=out.reasoning,
                alternatives=out.alternatives, assumptions=list(out.assumptions))
