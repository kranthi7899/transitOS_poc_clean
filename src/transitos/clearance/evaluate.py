"""Option evaluation: all per-block rules for every assignment, plus the rules that only exist across
assignments (double-booking, distinct spares consumed, accumulated operator hours).

`safe_evaluate` is the entry point everything else uses: any exception inside clearance excludes the
option and says so (fail closed)."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from transitos.contracts.clearance import ClearanceItem
from transitos.contracts.policy import AgencyPolicy

from .facts import Facts
from .rules import (
    EPS,
    block_hours,
    item,
    kill_switch_item,
    last_spare_item,
    min_distinct_spares,
    operator_items,
    overlaps,
    vehicle_items,
)

__all__ = ["Assign", "Clearance", "Option", "double_booking_item", "evaluate_option", "evaluate_vehicle_move",
           "kill_switch_item", "last_spare_item",
           "min_distinct_spares", "noteworthy", "operator_items", "safe_evaluate", "summarise", "unacknowledged",
           "vehicle_items"]


@dataclass(frozen=True)
class Assign:
    """One affected block moved to `spare_id`. `operator_id` None keeps the block's current operator."""
    block_id: str
    spare_id: str
    operator_id: str | None = None


@dataclass(frozen=True)
class Option:
    assignments: tuple[Assign, ...]

    def key(self) -> tuple:
        return tuple(sorted((a.block_id, a.spare_id, a.operator_id or "") for a in self.assignments))


@dataclass
class Clearance:
    items: list[ClearanceItem] = field(default_factory=list)
    excluded: bool = False
    warnings: int = 0
    unverifiable: int = 0
    tier: Literal["exact", "larger"] = "exact"

    @property
    def violations(self) -> list[ClearanceItem]:
        return [i for i in self.items if i.status == "violation"]


def _clearance(items: list[ClearanceItem]) -> Clearance:
    return Clearance(
        items=items,
        excluded=any(i.status == "violation" for i in items),
        warnings=sum(i.status == "warn" for i in items),
        unverifiable=sum(i.status == "unverifiable" for i in items),
        tier="larger" if any(i.rule == "vehicle_fit" and i.status == "warn" for i in items) else "exact")


def _eff_operator(a: Assign, facts: Facts) -> str | None:
    return a.operator_id or facts.blocks[a.block_id].operator_id


def double_booking_item(option: Option, facts: Facts) -> ClearanceItem:
    """Double-booking, judged inside the option (a vehicle or operator on overlapping blocks) and against the
    schedule feed (`facts.commitments`). No feed -> fails closed."""
    if facts.commitments is None:
        return item("double_booking", "violation", "unknown",
                    "schedule feed not available: double-booking cannot be ruled out (fails closed)")
    by_spare: dict[str, list[Assign]] = defaultdict(list)
    by_op: dict[str, list[Assign]] = defaultdict(list)
    for a in option.assignments:
        by_spare[a.spare_id].append(a)
        if (oid := _eff_operator(a, facts)) is not None:
            by_op[oid].append(a)
    clashes: list[str] = []
    for kind, groups in (("vehicle", by_spare), ("operator", by_op)):
        for rid, group in groups.items():
            g = sorted(group, key=lambda a: facts.blocks[a.block_id].start)
            for i, x in enumerate(g):
                for y in g[i + 1:]:
                    bx, by = facts.blocks[x.block_id], facts.blocks[y.block_id]
                    if overlaps(bx.start, bx.end, by.start, by.end):
                        clashes.append(f"{kind} {rid} is on overlapping blocks {bx.id} and {by.id}")
    for a in option.assignments:
        b = facts.blocks[a.block_id]
        keys = [f"vehicle:{a.spare_id}"] + ([f"operator:{o}"] if (o := _eff_operator(a, facts)) else [])
        for k in keys:
            for s, e, bid in facts.commitments.get(k, []):
                if bid != b.id and overlaps(b.start, b.end, s, e):
                    clashes.append(f"{k.replace(':', ' ')} is already on block {bid} during {b.id}")
    if clashes:
        return item("double_booking", "violation", "native", "; ".join(sorted(set(clashes))))
    return item("double_booking", "pass", "native")


def option_items(option: Option, facts: Facts, policy: AgencyPolicy) -> list[ClearanceItem]:
    out: list[ClearanceItem] = []
    if not option.assignments:
        return out
    by_spare: dict[str, list[Assign]] = defaultdict(list)
    by_op: dict[str, list[Assign]] = defaultdict(list)
    for a in option.assignments:
        by_spare[a.spare_id].append(a)
        if (oid := _eff_operator(a, facts)) is not None:
            by_op[oid].append(a)
    out.append(double_booking_item(option, facts))
    # operators driving several blocks accumulate hours
    lim = policy.limits
    for oid, group in by_op.items():
        op = facts.operators.get(oid)
        if op is None or len(group) < 2:
            continue
        total_block = sum(block_hours(facts.blocks[a.block_id]) for a in group)
        if facts.basis("workforce", "hours_today") != "unknown" and \
                op.hours_today + total_block > lim.max_operator_hours_day + EPS:
            out.append(item("operator_hours_day", "violation", facts.basis("workforce", "hours_today"),
                            f"operator_hours: {oid} {op.hours_today:.1f}h today + {total_block:.1f}h over "
                            f"{len(group)} blocks > max {lim.max_operator_hours_day:g}h"))
        if facts.basis("workforce", "hours_week") != "unknown" and \
                op.hours_week + total_block > lim.max_operator_hours_week + EPS:
            out.append(item("operator_hours_week", "violation", facts.basis("workforce", "hours_week"),
                            f"operator_hours_week: {oid} {op.hours_week:.1f}h + {total_block:.1f}h over "
                            f"{len(group)} blocks > max {lim.max_operator_hours_week:g}h"))
    out.append(last_spare_item(facts.ready_count, len(by_spare), lim.min_spares))
    return out


def evaluate_option(option: Option, facts: Facts, policy: AgencyPolicy, at: datetime) -> Clearance:
    """Judge a whole option. `at` is the planning time (kept for rules that depend on it; the current
    rules are all anchored on the blocks' own times)."""
    items: list[ClearanceItem] = []
    for a in option.assignments:
        b, v = facts.blocks[a.block_id], facts.vehicles[a.spare_id]
        items += vehicle_items(b, v, facts, policy)
        oid = _eff_operator(a, facts)
        if oid is None:
            continue
        op = facts.operators.get(oid)
        if op is None:
            items.append(item("operator_eligible", "violation", "native",
                              f"operator {oid} is not among the operators eligible for block {b.id}"))
        else:
            items += operator_items(b, op, v.type, facts, policy)
    items += option_items(option, facts, policy)
    return _clearance(items)


def evaluate_vehicle_move(block_id: str, spare_id: str, facts: Facts, policy: AgencyPolicy) -> Clearance:
    """One block onto one vehicle, without judging its operator: vehicle rules, double-booking, last spare.
    What the scheduling write adapter re-checks before `reassign_block`."""
    opt = Option((Assign(block_id, spare_id),))
    b, v = facts.blocks[block_id], facts.vehicles[spare_id]
    return _clearance(vehicle_items(b, v, facts, policy) + option_items(opt, facts, policy))


def safe_evaluate(option: Option, facts: Facts, policy: AgencyPolicy, at: datetime) -> Clearance:
    """Fail closed: any exception inside clearance excludes the option and records why."""
    try:
        return evaluate_option(option, facts, policy, at)
    except Exception as e:  # noqa: BLE001
        return Clearance(items=[item("clearance_error", "violation", "unknown",
                                     f"{type(e).__name__}: {e}"[:200])],
                         excluded=True)


def noteworthy(i: ClearanceItem) -> bool:
    """Items worth showing a human: anything but a clean native pass."""
    return not (i.status == "pass" and i.basis == "native")


def summarise(items: list[ClearanceItem]) -> list[ClearanceItem]:
    """Merge per-block/per-operator repeats of the same verdict into one line each, keeping only items that
    are not a clean native pass (plan, decision record and panel show these)."""
    groups: dict[tuple, list[str]] = {}
    ack: dict[tuple, bool] = {}
    for i in items:
        if not noteworthy(i):
            continue
        k = (i.rule, i.status, i.basis)
        groups.setdefault(k, [])
        if i.detail and i.detail not in groups[k]:
            groups[k].append(i.detail)
        ack[k] = ack.get(k, False) or i.requires_ack
    return [ClearanceItem(rule=r, status=s, basis=b, detail="; ".join(d)[:600], requires_ack=ack[(r, s, b)])
            for (r, s, b), d in groups.items()]


def unacknowledged(items: list[ClearanceItem], acknowledged: list[str]) -> list[str]:
    """Rule ids with a requires_ack item the approver has not acknowledged."""
    return sorted({i.rule for i in items if i.requires_ack} - set(acknowledged))
