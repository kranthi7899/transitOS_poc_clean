"""Ranking and search. Hard rules decide who is on the list; then rank by vehicle tier (exact before any
larger class, amendment A Q1), unverifiable count, warning count, soft score; keep the top three with a
one-line reason why #1 beat #2. Search is greedy per block plus a one-change neighbourhood, so the runners-up
are real, fully evaluated plans."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from transitos.contracts.objects import Block
from transitos.contracts.policy import AgencyPolicy

from .evaluate import Assign, Clearance, Option, safe_evaluate
from .facts import Facts


@dataclass
class RankedOption:
    option: Option
    clearance: Clearance
    score: float
    rank: int = 0


@dataclass
class RankedList:
    options: list[RankedOption] = field(default_factory=list)
    reason_first_beat_second: str = ""

    @property
    def top3(self) -> list[RankedOption]:
        return self.options[:3]


def soft_score(option: Option, facts: Facts) -> float:
    """Higher is better. +1 per block whose spare starts at the block's depot; a tiny penalty for the hours
    of operators newly assigned (spread the load)."""
    s = 0.0
    for a in option.assignments:
        b, v = facts.blocks[a.block_id], facts.vehicles[a.spare_id]
        s += 1.0 if v.depot == b.start_depot else 0.0
        if a.operator_id and (op := facts.operators.get(a.operator_id)):
            s -= 0.001 * op.hours_today
    return round(s, 6)


def _key(c: Clearance, score: float, idx: int) -> tuple:
    return (0 if c.tier == "exact" else 1, c.unverifiable, c.warnings, -score, idx)


def _reason(first: RankedOption, second: RankedOption) -> str:
    a, b = first.clearance, second.clearance
    if a.tier != b.tier:
        return "plan 1 beat plan 2: it uses the exact vehicle type, plan 2 needs a larger class"
    if a.unverifiable != b.unverifiable:
        return f"plan 1 beat plan 2: {a.unverifiable} vs {b.unverifiable} unverifiable item(s)"
    if a.warnings != b.warnings:
        return f"plan 1 beat plan 2: {a.warnings} vs {b.warnings} warning(s)"
    if first.score != second.score:
        return (f"plan 1 beat plan 2: equal clearance, higher soft score ({first.score:g} vs {second.score:g}: "
                "depot match, then lighter operator load)")
    return "plan 1 and plan 2 are equal on every ranking term; the earlier-listed plan was kept"


def rank_options(entries: Sequence[tuple[Option, Clearance, float]]) -> RankedList:
    """Drop excluded options, order the rest, number them, explain #1 versus #2."""
    kept = [(i, o, c, s) for i, (o, c, s) in enumerate(entries) if not c.excluded]
    kept.sort(key=lambda t: _key(t[2], t[3], t[0]))
    ranked = [RankedOption(o, c, s, n + 1) for n, (_, o, c, s) in enumerate(kept)]
    if not ranked:
        reason = "no option passed the hard rules"
    elif len(ranked) == 1:
        reason = "only one option passed the hard rules"
    else:
        reason = _reason(ranked[0], ranked[1])
    return RankedList(ranked, reason)


# ---- search ---------------------------------------------------------------------------------------
def greedy_option(blocks: Sequence[Block], spare_order: Mapping[str, Sequence[str]],
                  op_order: Mapping[str, Sequence[str]], facts: Facts, policy: AgencyPolicy,
                  at: datetime) -> Option | None:
    """One assignment per block, earliest block first. For each block take the candidate (spare, operator
    choice) with the best clearance, preferring an already-used spare (fewer distinct spares consumed), then
    the listing order. The block's own operator is tried first; others only when that fails everywhere."""
    chosen: list[Assign] = []
    for b in sorted(blocks, key=lambda x: (x.start, x.id)):
        used = {a.spare_id for a in chosen}
        best: tuple | None = None
        for stage in (0, 1):
            for si, s in enumerate(spare_order.get(b.id, [])):
                ops: list[str | None] = [None] if stage == 0 else \
                    [o for o in op_order.get(b.id, []) if o != b.operator_id]
                for oi, o in enumerate(ops):
                    cand = Assign(b.id, s, o)
                    c = safe_evaluate(Option((*chosen, cand)), facts, policy, at)
                    if c.excluded:
                        continue
                    k = (0 if c.tier == "exact" else 1, c.unverifiable, c.warnings, s not in used,
                         facts.vehicles[s].depot != b.start_depot, si, oi)
                    if best is None or k < best[0]:
                        best = (k, cand)
            if best is not None:
                break
        if best is None:
            return None
        chosen.append(best[1])
    return Option(tuple(chosen))


def neighbours(base: Option, spare_order: Mapping[str, Sequence[str]], op_order: Mapping[str, Sequence[str]],
               facts: Facts) -> list[Option]:
    """Every option that differs from `base` in exactly one block's spare or operator."""
    out: list[Option] = []
    for i, a in enumerate(base.assignments):
        b = facts.blocks[a.block_id]
        for s in spare_order.get(a.block_id, []):
            if s != a.spare_id:
                out.append(Option(base.assignments[:i] + (Assign(a.block_id, s, a.operator_id),)
                                  + base.assignments[i + 1:]))
        for o in op_order.get(a.block_id, []):
            if o != a.operator_id and o != b.operator_id:
                out.append(Option(base.assignments[:i] + (Assign(a.block_id, a.spare_id, o),)
                                  + base.assignments[i + 1:]))
    return out


def rank_plans(seeds: Sequence[Option], blocks: Sequence[Block], spare_order: Mapping[str, Sequence[str]],
               op_order: Mapping[str, Sequence[str]], facts: Facts, policy: AgencyPolicy,
               at: datetime) -> RankedList:
    """Evaluate the seeds (e.g. the model's proposal) plus a greedy plan and the one-change neighbourhood of
    each; rank everything that passes the hard rules."""
    pool: list[Option] = list(seeds)
    g = greedy_option(blocks, spare_order, op_order, facts, policy, at)
    if g is not None:
        pool.append(g)
    for base in list(pool):
        pool += neighbours(base, spare_order, op_order, facts)
    seen: set[tuple] = set()
    entries: list[tuple[Option, Clearance, float]] = []
    for o in pool:
        if o.key() in seen:
            continue
        seen.add(o.key())
        c = safe_evaluate(o, facts, policy, at)
        entries.append((o, c, soft_score(o, facts) if not c.excluded else 0.0))
    return rank_options(entries)
