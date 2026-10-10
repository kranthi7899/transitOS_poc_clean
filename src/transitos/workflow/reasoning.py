"""LLM access point (CLAUDE.md rule 7). The workflow only talks to a `Reasoner`.
Default is `StubReasoner`; `code_ranked_fallback` is the deterministic safety net.
Model output is NEVER trusted: `validate_output` checks it against code-computed feasible lists."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field, ValidationError


class Assignment(BaseModel):
    block_id: str
    spare_id: str | None = None
    operator_id: str | None = None


class ModelOutput(BaseModel):
    decision: Literal["pull_now", "pull_at_block_end", "monitor"]
    pull_at: datetime | None = None
    assignments: list[Assignment] = Field(default_factory=list)
    reasoning: str = ""
    alternatives: list[dict[str, Any]] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)


class Reasoner(Protocol):
    name: str
    prompt_version: str

    def propose(self, facts: dict, attempt: int = 0) -> dict:
        """Return a raw dict shaped like ModelOutput. `facts` carries the feasible lists
        (key `blocks`: block_id, spares, operators, needs_operator), `now`, `window_end`,
        `trigger_type`, `previous_errors`."""
        ...


def _parse(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _required_blocks(facts: dict, pull_at: datetime) -> list[dict]:
    return [b for b in facts["blocks"] if _parse(b["end"]) > pull_at]


def _assign(facts: dict, pull_at: datetime, rank) -> list[dict]:
    used_s: set[str] = set()
    used_o: set[str] = set()
    out = []
    for b in _required_blocks(facts, pull_at):
        spares = [s for s in rank("spare", b, b["spares"]) if s["id"] not in used_s]
        spare = spares[0]["id"] if spares else None
        if spare:
            used_s.add(spare)
        op = None
        if b["needs_operator"]:
            ops = [o for o in rank("operator", b, b["operators"]) if o["id"] not in used_o]
            op = ops[0]["id"] if ops else None
            if op:
                used_o.add(op)
        out.append({"block_id": b["block_id"], "spare_id": spare, "operator_id": op})
    return out


class StubReasoner:
    """Deterministic stand-in for a model: first feasible choice in listed order."""
    name = "stub"
    prompt_version = "v0"

    def propose(self, facts: dict, attempt: int = 0) -> dict:
        now = _parse(facts["now"])
        blocks = sorted(facts["blocks"], key=lambda b: b["end"])
        if facts.get("trigger_type") == "PMOverdue" and blocks:
            decision, pull_at = "pull_at_block_end", _parse(blocks[0]["end"])
        else:
            decision, pull_at = "pull_now", now
        return {
            "decision": decision, "pull_at": pull_at.isoformat(),
            "assignments": _assign(facts, pull_at, lambda _k, _b, xs: xs),
            "reasoning": f"stub: {decision}; first feasible spare/operator per block",
            "alternatives": [], "assumptions": ["stub reasoner, no model call"],
        }


def code_ranked_fallback(facts: dict) -> dict:
    """Always pull now; rank spares by depot match then id, operators by fewest hours then id."""
    now = _parse(facts["now"])

    def rank(kind, block, xs):
        if kind == "spare":
            return sorted(xs, key=lambda s: (s.get("depot") != block.get("start_depot"), s["id"]))
        return sorted(xs, key=lambda o: (o.get("hours_today", 0.0), o["id"]))

    return {
        "decision": "pull_now", "pull_at": now.isoformat(),
        "assignments": _assign(facts, now, rank),
        "reasoning": "code-ranked fallback: depot match, then least-hours operator",
        "alternatives": [], "assumptions": ["fallback used: model output was missing or infeasible"],
    }


def validate_output(raw: Any, facts: dict) -> tuple[ModelOutput | None, list[str]]:
    """Schema + feasibility. Returns (output, errors); output is None on schema failure."""
    try:
        out = ModelOutput.model_validate(raw)
    except ValidationError as e:
        return None, [f"schema:{e.error_count()} errors"]
    except Exception as e:  # noqa: BLE001
        return None, [f"schema:{e}"]
    errs: list[str] = []
    now, wend = _parse(facts["now"]), _parse(facts["window_end"])
    if out.decision == "monitor":
        return out, errs
    if out.decision == "pull_at_block_end" and out.pull_at is None:
        errs.append("pull_at_missing")
    pull_at = out.pull_at if out.pull_at is not None else now
    if pull_at.tzinfo is None and now.tzinfo is not None:
        errs.append("pull_at_naive_datetime")
        return out, errs
    if not (now <= pull_at <= wend):
        errs.append("pull_at_outside_window")
        return out, errs
    by_block = {b["block_id"]: b for b in facts["blocks"]}
    seen_blocks: set[str] = set()
    used_s: set[str] = set()
    used_o: set[str] = set()
    for a in out.assignments:
        b = by_block.get(a.block_id)
        if b is None:
            errs.append(f"unknown_block:{a.block_id}")
            continue
        if a.block_id in seen_blocks:
            errs.append(f"duplicate_assignment:{a.block_id}")
        seen_blocks.add(a.block_id)
        if a.spare_id not in {s["id"] for s in b["spares"]}:
            errs.append(f"spare_not_feasible:{a.block_id}:{a.spare_id}")
        elif a.spare_id in used_s:
            errs.append(f"spare_reused:{a.spare_id}")
        used_s.add(a.spare_id or "")
        if b["needs_operator"]:
            if a.operator_id not in {o["id"] for o in b["operators"]}:
                errs.append(f"operator_not_feasible:{a.block_id}:{a.operator_id}")
            elif a.operator_id in used_o:
                errs.append(f"operator_reused:{a.operator_id}")
            used_o.add(a.operator_id or "")
        elif a.operator_id not in (None, b.get("current_operator")):
            errs.append(f"operator_unneeded:{a.block_id}")
    for b in _required_blocks(facts, pull_at):
        if b["block_id"] not in seen_blocks:
            errs.append(f"missing_assignment:{b['block_id']}")
    if facts["ready_count"] - len(used_s - {""}) < facts["min_spares"]:
        errs.append("last_spare_rule")
    return out, errs
