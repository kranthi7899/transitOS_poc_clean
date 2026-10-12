"""Q4 (owner decision, A179-A182): when a block has no feasible operator and a candidate's time in the product's data
was a repeated (fall-back) wall-clock time with no offset, the escalation asks a person WHICH instant is meant and
hands them the closest options: the candidates that would be feasible under each reading.

Pure functions over plain data. Feasibility is never judged here: every option is the existing clearance
evaluation (`safe_evaluate`) of a whole re-plan of the block on a COPY of the facts in which only the ambiguous field
of that one candidate carries the alternative instant (A181)."""
from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping
from datetime import datetime

from transitos.clearance.evaluate import Assign, Option, safe_evaluate, summarise
from transitos.clearance.facts import Facts
from transitos.contracts.objects import Operator
from transitos.contracts.policy import AgencyPolicy

KIND = "ambiguous_local_time"


def _instant(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _clock(wall: datetime) -> str:
    return wall.strftime("%H:%M" if wall.second == 0 else "%H:%M:%S")


def short_instant(reading: str, local: str) -> str:
    """'05:00Z' when the instant falls on the local wall time's own date, else '2026-04-04T15:30Z'."""
    t, wall = _instant(reading), datetime.fromisoformat(local)
    hm = _clock(t)
    return f"{hm}Z" if t.date() == wall.date() else f"{t.date().isoformat()}T{hm}Z"


def question_text(local: str, tz: str, readings: list[str]) -> str:
    """The plain question, naming the wall time and date exactly as the product's data has them."""
    wall = datetime.fromisoformat(local)
    hm = _clock(wall)
    times = " and ".join(short_instant(r, local) for r in readings)
    return f"Which {hm} is meant? {hm} on {wall.date().isoformat()} ({tz}) occurs twice: {times}"


def with_reading(op: Operator, field: str, key: str | None, at: datetime) -> Operator:
    """A copy of the operator with ONE time field (or one credential's expiry) set to `at`; nothing else changes."""
    if field == "training_valid_to":
        return op.model_copy(update={"training_valid_to": {**op.training_valid_to, str(key): at}})
    if field not in Operator.model_fields or field in ("id", "unknown_fields"):
        raise ValueError(f"not an operator time field: {field!r}")
    return op.model_copy(update={field: at})


def _option(block_id: str, op: Operator, reading: str, local: str, spares: Iterable[str], facts: Facts,
            policy: AgencyPolicy, at: datetime) -> dict | None:
    """The best whole re-plan of the block with this operator (fewest unverifiable/warn items, then spare id), or
    None when no spare makes it feasible. Clearance judges it; a clearance exception excludes it (fail closed)."""
    trial = dataclasses.replace(facts, operators={**facts.operators, op.id: op})
    best: tuple[tuple[int, str], dict] | None = None
    for sid in spares:
        cl = safe_evaluate(Option((Assign(block_id, sid, op.id),)), trial, policy, at)
        if cl.excluded:
            continue
        ack = sorted({i.rule for i in cl.items if i.requires_ack})
        rank = (cl.unverifiable + cl.warnings, sid)
        if best is None or rank < best[0]:
            wall = _clock(datetime.fromisoformat(local))
            text = (f"{op.id} if {wall} means {short_instant(reading, local)}: on spare {sid} for {block_id}; "
                    + (f"still to acknowledge: {', '.join(ack)}" if ack else "nothing else to acknowledge"))
            best = (rank, {"operator": op.id, "product": "workforce", "reading": reading, "spare": sid,
                           "unverifiable": cl.unverifiable, "warnings": cl.warnings, "still_to_acknowledge": ack,
                           "notes": [f"{i.rule}: {i.status} ({i.basis}) {i.detail}".strip() for i in summarise(cl.items)],
                           "text": text})
    return None if best is None else best[1]


def needs_human_for(ambiguities: list[dict], blocked: Iterable[str], eligible: Mapping[str, set[str]],
                    spares: Mapping[str, list[str]], facts: Facts, policy: AgencyPolicy, at: datetime) -> list[dict]:
    """One entry per (block with no feasible operator, ambiguous wall time and field): the question, every operator
    whose cell says that time and who is not eligible for the block (`subjects`), both readings and the one the
    rules used, and the options (A180). An empty option list is said in words (`no_option`)."""
    out: list[dict] = []
    for bid in blocked:
        groups: dict[tuple, list[dict]] = {}
        for a in ambiguities:
            if a.get("product") != "workforce" or a["subject"] in eligible.get(bid, set()):
                continue
            groups.setdefault((a["field"], a.get("key") or "", a["local"], a["timezone"]), []).append(a)
        for (fld, key, local, tz), entries in sorted(groups.items()):
            entries = sorted(entries, key=lambda a: a["subject"])
            readings, read_as = entries[0]["readings"], entries[0]["read_as"]
            options: list[dict] = []
            for a in entries:
                op = Operator.model_validate(a["record"])
                for r in a["readings"]:
                    o = _option(bid, with_reading(op, fld, key or None, _instant(r)), r, local,
                                spares.get(bid, []), facts, policy, at)
                    if o is not None:
                        options.append(o)
            options.sort(key=lambda o: (o["unverifiable"] + o["warnings"], o["operator"], o["reading"]))
            ids = ", ".join(a["subject"] for a in entries)
            entry = {
                "kind": KIND, "question": question_text(local, tz, readings), "block": bid,
                "product": "workforce", "field": fld, "key": key or None, "local": local, "timezone": tz,
                "subjects": [{"product": "workforce", "id": a["subject"]} for a in entries],
                "readings": list(readings), "read_as": read_as,
                "why": (f"the workforce export gives {fld} as {local} with no offset; TransitOS read it as "
                        f"{short_instant(read_as, local)} (the conservative reading), so {ids} could not be shown "
                        f"feasible for {bid}"),
                "options": options,
            }
            if not options:
                entry["no_option"] = (f"No candidate would be feasible for {bid} under either reading"
                                      + ("" if spares.get(bid) else " (no spare vehicle is feasible for it either)"))
            out.append(entry)
    return out


def render_text(entries: list[dict]) -> str:
    """The same content as plain lines (panel, report, `expect_escalation_contains`)."""
    lines: list[str] = []
    for e in entries:
        lines.append(e["question"])
        lines.append(f"Why: {e['why']}")
        lines += [f"Option: {o['text']}" for o in e["options"]]
        if e.get("no_option"):
            lines.append(e["no_option"])
    return "\n".join(lines)


__all__ = ["KIND", "needs_human_for", "question_text", "render_text", "short_instant", "with_reading"]
