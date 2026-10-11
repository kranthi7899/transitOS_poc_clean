"""The rules. Each returns `ClearanceItem`s; none raises on missing data (it reports `unverifiable` or
`not_evaluated` with basis `unknown`). Pure: facts in, items out.

Classification (amendment A): hard safety/legal rules exclude only on a KNOWN violation; unknown data on them
is `unverifiable` (stays on the list, ranks lower, needs approver acknowledgement); optional rules (rest,
spread, medical card) are `not_evaluated` without data and `warn` on a known breach."""
from __future__ import annotations

from datetime import UTC, datetime

from transitos.contracts.clearance import Basis, ClearanceItem, ClearanceStatus
from transitos.contracts.objects import Block, Operator, Vehicle
from transitos.contracts.policy import AgencyPolicy

from .facts import Facts

EPS = 1e-9


def item(rule: str, status: ClearanceStatus, basis: Basis, detail: str = "", ack: bool = False) -> ClearanceItem:
    return ClearanceItem(rule=rule, status=status, basis=basis, detail=detail, requires_ack=ack)


def unverifiable(rule: str, why: str) -> ClearanceItem:
    return item(rule, "unverifiable", "unknown", why, True)


def block_hours(b: Block) -> float:
    return (b.end - b.start).total_seconds() / 3600.0


def overlaps(s1: datetime, e1: datetime, s2: datetime, e2: datetime) -> bool:
    return s1 < e2 and s2 < e1


# ---- vehicle rules --------------------------------------------------------------------------------
def _capacity(facts: Facts, rule: str, field: str, short: bool, detail: str, label: str) -> ClearanceItem:
    basis = facts.basis("scheduling", field)
    if basis == "unknown":
        return unverifiable(rule, f"scheduling does not supply {field}; cannot check {label}")
    return item(rule, "violation" if short else "pass", basis, detail if short else "")


def vehicle_fit_item(block: Block, v: Vehicle, facts: Facts, policy: AgencyPolicy) -> ClearanceItem | None:
    """Graded fit (Q1): exact is best; a larger class is a warning needing acknowledgement (or an exclusion
    when the agency sets vehicle_fit=exact); a smaller class, or classes we cannot order, never qualify."""
    need = block.needs.type
    if need is None:
        return None
    basis = facts.basis("scheduling", "type")
    if basis == "unknown":
        return unverifiable("vehicle_fit", "scheduling does not supply the vehicle type")
    if v.type == need:
        return item("vehicle_fit", "pass", basis, f"{v.id}: exact type {need}")
    order = facts.tables.vehicle_order
    if need in order and v.type in order:
        if order.index(v.type) > order.index(need):
            if policy.vehicle_fit == "exact":
                return item("vehicle_fit", "violation", basis,
                            f"{v.id}: larger class {v.type} for a {need} block; agency vehicle_fit is exact")
            return item("vehicle_fit", "warn", basis,
                        f"{v.id}: larger class {v.type} used for a {need} block (fallback tier)", True)
        return item("vehicle_fit", "violation", basis, f"{v.id}: smaller class {v.type} than required {need}")
    return item("vehicle_fit", "violation", basis,
                f"{v.id}: type {v.type} cannot be ordered against required {need}")


def vehicle_length_item(block: Block, v: Vehicle, facts: Facts) -> ClearanceItem | None:
    limit = block.needs.max_vehicle_length_ft
    if limit is None:
        return None
    if v.length_ft is not None and facts.basis("scheduling", "length_ft") != "unknown":
        length, basis, how = v.length_ft, facts.basis("scheduling", "length_ft"), "recorded length"
    elif v.type in facts.tables.typical_length_ft:
        length, basis = facts.tables.typical_length_ft[v.type], "assumed"
        how = f"assumed typical length for class {v.type} (A66)"
    else:
        return unverifiable("vehicle_length", f"{v.id}: no length on record and none assumed for type {v.type}")
    over = length > limit + EPS
    return item("vehicle_length", "violation" if over else "pass", basis,
                f"{v.id}: {how} {length:g} ft vs block limit {limit:g} ft")


def vehicle_items(block: Block, v: Vehicle, facts: Facts, policy: AgencyPolicy) -> list[ClearanceItem]:
    n = block.needs
    out = [item("vehicle_ready", "pass" if v.status == "ready" else "violation", "native",
                "" if v.status == "ready" else f"{v.id} is {v.status}, not ready")]
    if n.seats:
        out.append(_capacity(facts, "vehicle_seats", "seats", v.seats < n.seats,
                             f"{v.id} has {v.seats} seats, needs {n.seats}", "seats"))
    if n.wheelchair_positions:
        out.append(_capacity(facts, "vehicle_wheelchair", "wheelchair_positions",
                             v.wheelchair_positions < n.wheelchair_positions,
                             f"{v.id} has {v.wheelchair_positions} wheelchair positions, needs {n.wheelchair_positions}",
                             "wheelchair positions"))
    if n.lift:
        out.append(_capacity(facts, "vehicle_lift", "lift_ok", not v.lift_ok,
                             f"{v.id} lift not operational", "the lift"))
    for extra in (vehicle_fit_item(block, v, facts, policy), vehicle_length_item(block, v, facts)):
        if extra is not None:
            out.append(extra)
    return out


# ---- operator rules -------------------------------------------------------------------------------
def _expired(valid_to: datetime, block: Block) -> bool:
    """A credential whose valid_to falls on day D is not valid for work on D or later (Review Focus 1:
    a violation on the day it expires, not the day after). Dates are taken in valid_to's own zone."""
    tz = valid_to.tzinfo or UTC
    work_day = block.end.astimezone(tz).date() if block.end.tzinfo else block.end.date()
    return work_day >= valid_to.date()


def day_hours_item(op_id: str, hours_today: float, block_hrs: float, limit: float, basis: Basis = "native") -> ClearanceItem:
    """Daily hours cap, inclusive at the limit (10.0h is allowed, 10.1h is not)."""
    total = hours_today + block_hrs
    bad = total > limit + EPS
    return item("operator_hours_day", "violation" if bad else "pass", basis,
                f"operator_hours: {op_id} {hours_today:.1f}h today + {block_hrs:.1f}h block = {total:.1f}h "
                f"> max {limit:g}h" if bad else "")


def lift_training_item(op: Operator, basis: Basis) -> ClearanceItem:
    if basis == "unknown":
        return unverifiable("operator_lift_training", f"{op.id}: workforce does not supply lift training")
    return item("operator_lift_training", "pass" if op.lift_trained else "violation", basis,
                "" if op.lift_trained else
                f"lift_qualification: operator {op.id} is not lift-trained but block needs a lift")


def _hours_items(block: Block, op: Operator, facts: Facts, policy: AgencyPolicy) -> list[ClearanceItem]:
    lim, bh = policy.limits, block_hours(block)
    today_b, week_b = facts.basis("workforce", "hours_today"), facts.basis("workforce", "hours_week")
    out: list[ClearanceItem] = []
    if today_b == "unknown":
        out.append(unverifiable("operator_hours_day", f"{op.id}: workforce does not supply hours worked today"))
    else:
        out.append(day_hours_item(op.id, op.hours_today, bh, lim.max_operator_hours_day, today_b))
    cap = lim.max_operator_hours_week
    if week_b != "unknown":
        total = op.hours_week + bh
        bad = total > cap + EPS
        out.append(item("operator_hours_week", "violation" if bad else "pass", week_b,
                        f"operator_hours_week: {op.id} {op.hours_week:.1f}h in the 8-day window + {bh:.1f}h block "
                        f"= {total:.1f}h > max {cap:g}h" if bad else ""))
    elif today_b != "unknown" and op.hours_today + bh > cap + EPS:
        out.append(item("operator_hours_week", "violation", "derived",
                        f"operator_hours_week: {op.id} weekly hours not supplied, but today's {op.hours_today:.1f}h "
                        f"+ {bh:.1f}h block already exceeds max {cap:g}h (lower bound)"))
    else:
        out.append(unverifiable("operator_hours_week", f"{op.id}: workforce does not supply hours in the 8-day window"))
    return out


def _qualification_items(block: Block, op: Operator, vehicle_type: str | None, facts: Facts) -> list[ClearanceItem]:
    wb = lambda f: facts.basis("workforce", f)
    n = block.needs
    out: list[ClearanceItem] = []
    if wb("licence_class") != "unknown":                       # new qualification model (D-1)
        req = facts.tables.vehicle_requirements.get(vehicle_type) if vehicle_type else None
        if vehicle_type is not None and req is None:
            for rule in ("operator_licence_class", "operator_endorsement", "operator_vehicle_training"):
                out.append(unverifiable(rule, f"{op.id}: no licence requirement on record for vehicle type {vehicle_type}"))
        elif req is not None:
            out += _new_model_items(block, op, vehicle_type, req, facts)
    elif wb("qualifications") != "unknown" and op.qualifications:   # legacy list
        vt = vehicle_type or n.type
        if vt is not None:
            ok = vt in op.qualifications
            out.append(item("operator_qualification", "pass" if ok else "violation", wb("qualifications"),
                            "" if ok else f"operator_qualification: {op.id} lacks {vt}"))
    else:
        out.append(unverifiable("operator_qualification",
                                f"{op.id}: workforce supplies no qualification data (licence, endorsement or training)"))
    if n.lift:
        out.append(lift_training_item(op, wb("lift_trained")))
    return out


def _new_model_items(block: Block, op: Operator, vtype: str, req, facts: Facts) -> list[ClearanceItem]:
    wb = lambda f: facts.basis("workforce", f)
    out: list[ClearanceItem] = []
    order = facts.tables.licence_order
    if op.licence_class is None:
        out.append(unverifiable("operator_licence_class", f"{op.id}: licence class not on record"))
    else:
        need = req["min_licence_class"]
        ok = order.index(op.licence_class) >= order.index(need)
        out.append(item("operator_licence_class", "pass" if ok else "violation", wb("licence_class"),
                        "" if ok else f"{op.id}: licence class {op.licence_class} below {need} needed for {vtype}"))
    code = facts.tables.passenger_endorsement_code
    needs_p = bool(req.get("passenger_endorsement"))
    held_p = code in op.endorsements
    if needs_p:
        eb = wb("endorsements")
        if eb == "unknown":
            out.append(unverifiable("operator_endorsement", f"{op.id}: workforce does not supply endorsements"))
        else:
            out.append(item("operator_endorsement", "pass" if held_p else "violation", eb,
                            "" if held_p else f"{op.id}: passenger endorsement {code} missing"))
    train = req["training_type"]
    held_t = train in op.trained_vehicle_types
    tb = wb("trained_vehicle_types")
    if tb == "unknown":
        out.append(unverifiable("operator_vehicle_training", f"{op.id}: workforce does not supply trained vehicle types"))
    else:
        out.append(item("operator_vehicle_training", "pass" if held_t else "violation", tb,
                        "" if held_t else f"{op.id}: not trained on vehicle type {train}"))
    creds = [c for c, held in ((code, needs_p and held_p), (train, held_t),
                               ("lift", block.needs.lift and op.lift_trained)) if held]
    if creds:
        out.append(_expiry_item(block, op, creds, wb("training_valid_to")))
    return out


def _expiry_item(block: Block, op: Operator, creds: list[str], basis: Basis) -> ClearanceItem:
    if basis == "unknown":
        return unverifiable("operator_training_expiry", f"{op.id}: workforce does not supply expiry dates")
    expired = [c for c in creds if c in op.training_valid_to and _expired(op.training_valid_to[c], block)]
    undated = [c for c in creds if c not in op.training_valid_to]
    if expired:
        return item("operator_training_expiry", "violation", basis,
                    f"{op.id}: {', '.join(expired)} expired on or before the block's day")
    if undated:
        return unverifiable("operator_training_expiry", f"{op.id}: no expiry date on record for {', '.join(undated)}")
    return item("operator_training_expiry", "pass", basis)


def operator_items(block: Block, op: Operator, vehicle_type: str | None, facts: Facts,
                   policy: AgencyPolicy) -> list[ClearanceItem]:
    """All operator rules for one block. `vehicle_type` is the type of the vehicle that will run it
    (None while the vehicle is not chosen yet: the type-dependent qualification rules are then skipped
    and re-run at option level)."""
    wb = lambda f: facts.basis("workforce", f)
    lim = policy.limits
    out = _hours_items(block, op, facts, policy)
    out += _qualification_items(block, op, vehicle_type, facts)
    ab = wb("availability")
    if ab == "unknown":
        out.append(unverifiable("operator_availability", f"{op.id}: workforce does not supply an availability window"))
    else:
        bad = (op.available_from is not None and op.available_from > block.start) or \
              (op.available_to is not None and op.available_to < block.end)
        out.append(item("operator_availability", "violation" if bad else "pass", ab,
                        f"operator_not_available: {op.id} outside the block window" if bad else ""))
    # optional rules
    if wb("medical_valid_to") == "unknown" or op.medical_valid_to is None:
        out.append(item("operator_medical", "not_evaluated", "unknown", f"{op.id}: no medical card date on record"))
    else:
        bad = _expired(op.medical_valid_to, block)
        out.append(item("operator_medical", "violation" if bad else "pass", wb("medical_valid_to"),
                        f"{op.id}: medical card expired" if bad else ""))
    if wb("duty_times") == "unknown" or op.last_duty_end is None:
        out.append(item("operator_rest", "not_evaluated", "unknown", f"{op.id}: previous duty end not on record"))
    else:
        rest = (block.start - op.last_duty_end).total_seconds() / 3600.0
        short = rest < lim.min_rest_hours - EPS
        out.append(item("operator_rest", "warn" if short else "pass", wb("duty_times"),
                        f"{op.id}: {rest:.1f}h rest before the block, minimum {lim.min_rest_hours:g}h" if short else "",
                        short))
    if wb("duty_times") == "unknown" or op.duty_start_today is None:
        out.append(item("operator_spread", "not_evaluated", "unknown", f"{op.id}: duty start not on record"))
    else:
        spread = (block.end - op.duty_start_today).total_seconds() / 3600.0
        long = spread > lim.max_spread_hours + EPS
        out.append(item("operator_spread", "warn" if long else "pass", wb("duty_times"),
                        f"{op.id}: duty spread {spread:.1f}h, maximum {lim.max_spread_hours:g}h" if long else "",
                        long))
    return out


# ---- spares, kill switch, double booking ----------------------------------------------------------
def last_spare_item(ready_count: int | None, distinct_used: int, min_spares: int) -> ClearanceItem:
    """Last-spare rule (A25, A49 closed): using `distinct_used` distinct spares must leave min_spares ready."""
    if ready_count is None:
        return unverifiable("last_spare", "ready spare count not available")
    remaining = ready_count - distinct_used
    if remaining < min_spares:
        return item("last_spare", "violation", "native",
                    f"min_spares: {remaining} spare(s) would remain after using {distinct_used} distinct "
                    f"spare(s), minimum is {min_spares}")
    return item("last_spare", "pass", "native",
                f"{distinct_used} distinct spare(s) consumed, {remaining} left (minimum {min_spares})")


def kill_switch_item(on: bool) -> ClearanceItem:
    """Fails closed: the write adapters stop on any 'on'."""
    return item("kill_switch", "violation" if on else "pass", "native", "writes disabled" if on else "")


def min_distinct_spares(blocks: list[Block], candidates: dict[str, list[str]]) -> int | None:
    """Fewest distinct spares that can cover all blocks when a spare may serve several non-overlapping
    blocks (greedy interval assignment, earliest start first, reuse before consuming a new spare).
    None when some block has no candidate."""
    used: dict[str, list[tuple[datetime, datetime]]] = {}
    for b in sorted(blocks, key=lambda x: (x.start, x.id)):
        cands = candidates.get(b.id, [])
        if not cands:
            return None
        pick = next((s for s in cands if s in used
                     and not any(overlaps(b.start, b.end, s0, e0) for s0, e0 in used[s])), None)
        if pick is None:
            pick = next((s for s in cands if s not in used), None)
        if pick is None:
            return None
        used.setdefault(pick, []).append((b.start, b.end))
    return len(used)
