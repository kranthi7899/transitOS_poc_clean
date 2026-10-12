"""The rules. Each returns `ClearanceItem`s; none raises on missing data (it reports `unverifiable` or
`not_evaluated` with basis `unknown`). Pure: facts in, items out.

Classification (amendment A; Step 1b rulings): hard safety/legal rules exclude only on a KNOWN violation;
unknown data on them is `unverifiable` (stays on the list, ranks lower, needs approver acknowledgement); the
medical card is a legal rule (F8). Optional rules (rest, spread) are `not_evaluated` without data and `warn` on
a known breach.

Missing data is resolved PER RECORD (C-2, F3): a contract field listed in the record's `unknown_fields` (the
source left it blank or absent) is unknown for that record, ahead of the product's capability profile; a block
need listed in `Needs.unknown_fields` was not declared, so "no requirement" is never assumed. An item's basis is
the weakest basis of every input it read, so a pass is a clean native pass only when all its inputs were native."""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from transitos.contracts.clearance import Basis, ClearanceItem, ClearanceStatus
from transitos.contracts.objects import Block, Operator, Vehicle
from transitos.contracts.policy import AgencyPolicy

from .facts import Datum, Facts, weakest

EPS = 1e-9
NEW_QUALIFICATION_FIELDS = ("licence_class", "endorsements", "trained_vehicle_types", "training_valid_to")


def item(rule: str, status: ClearanceStatus, basis: Basis, detail: str = "", ack: bool = False) -> ClearanceItem:
    return ClearanceItem(rule=rule, status=status, basis=basis, detail=detail, requires_ack=ack)


def unverifiable(rule: str, why: str) -> ClearanceItem:
    return item(rule, "unverifiable", "unknown", why, True)


def clearance_error(where: str, e: BaseException) -> ClearanceItem:
    """Fail closed (amendment A Q2): an exception inside clearance is a violation for the candidate it was
    judging, never a crash of the caller and never a pass."""
    return item("clearance_error", "violation", "unknown",
                f"clearance_error in {where}: {type(e).__name__}: {e}"[:200])


def guarded(where: str, fn: Callable[..., list[ClearanceItem]], *args) -> list[ClearanceItem]:
    """Run one direct rule call (vehicle_items / operator_items / ...) for ONE candidate; any exception excludes
    that candidate with a `clearance_error` item (F4, A133) instead of propagating."""
    try:
        return fn(*args)
    except Exception as e:  # noqa: BLE001
        return [clearance_error(where, e)]


def block_hours(b: Block) -> float:
    return (b.end - b.start).total_seconds() / 3600.0


def overlaps(s1: datetime, e1: datetime, s2: datetime, e2: datetime) -> bool:
    """Half-open intervals [start, end): blocks that touch end-to-start do not overlap (A125)."""
    return s1 < e2 and s2 < e1


def need_unknown(block: Block, key: str) -> bool:
    """The block did not declare this need (C-2): 'no requirement' must not be assumed."""
    return key in (block.needs.unknown_fields or ())


# ---- vehicle rules --------------------------------------------------------------------------------
def _capacity(block: Block, v: Vehicle, facts: Facts, rule: str, field: str, need_key: str, short: Callable[[], bool],
              detail: Callable[[], str], label: str) -> ClearanceItem:
    if need_unknown(block, need_key):
        return unverifiable(rule, f"block {block.id} does not declare its {need_key} need; cannot check {label}")
    basis = facts.record_basis("scheduling", field, v)
    if basis == "unknown":
        return unverifiable(rule, f"{v.id}: scheduling does not supply {field}; cannot check {label}")
    bad = short()
    return item(rule, "violation" if bad else "pass", basis, detail() if bad else "")


def vehicle_fit_item(block: Block, v: Vehicle, facts: Facts, policy: AgencyPolicy) -> ClearanceItem | None:
    """Graded fit (Q1): exact is best; a larger class is a warning needing acknowledgement (or an exclusion
    when the agency sets vehicle_fit=exact); a smaller class, or classes we cannot order, never qualify."""
    if need_unknown(block, "type"):
        return unverifiable("vehicle_fit", f"block {block.id} does not declare its vehicle type need")
    need = block.needs.type
    if need is None:
        return None
    basis = facts.record_basis("scheduling", "type", v)
    if basis == "unknown":
        return unverifiable("vehicle_fit", f"{v.id}: scheduling does not supply the vehicle type")
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


def resolve_length(v: Vehicle, facts: Facts) -> Datum:
    """Vehicle length in the resolution order: native record, then the tagged class default (A66), else unknown.
    The class default needs a KNOWN type."""
    lb = facts.record_basis("scheduling", "length_ft", v)
    if v.length_ft is not None and lb != "unknown":
        return Datum(v.length_ft, lb, "recorded length")
    if facts.record_basis("scheduling", "type", v) != "unknown" and v.type in facts.tables.typical_length_ft:
        return Datum(facts.tables.typical_length_ft[v.type], "assumed",
                     f"assumed typical length for class {v.type} (A66)")
    return Datum(None, "unknown", f"no length on record and none assumed for type {v.type}")


def vehicle_length_item(block: Block, v: Vehicle, facts: Facts) -> ClearanceItem | None:
    if need_unknown(block, "max_vehicle_length_ft"):
        return unverifiable("vehicle_length", f"block {block.id} does not say whether it has a length limit")
    limit = block.needs.max_vehicle_length_ft
    if limit is None:
        return None
    length = resolve_length(v, facts)
    if length.value is None:
        return unverifiable("vehicle_length", f"{v.id}: {length.note}")
    over = length.value > limit + EPS   # a breach on an ASSUMED default excludes and says it is assumed (A70; F9)
    return item("vehicle_length", "violation" if over else "pass", length.basis,
                f"{v.id}: {length.note} {length.value:g} ft vs block limit {limit:g} ft")


def asset_active_item(v: Vehicle, facts: Facts) -> ClearanceItem:
    """S2 fix round F4 (Review Focus 5, A142), safety class: the vehicle's MAINTENANCE asset must be active.
    Retired = violation (never a spare); unknown (no feed, no maintenance asset, maintenance unreachable, a blank or
    unrecognised status) = unverifiable, needing acknowledgement; active = pass."""
    if facts.asset_status is None:
        return unverifiable("asset_active", f"{v.id}: no maintenance asset-status feed")
    status = (facts.asset_status.get(v.id) or "").strip().lower()
    if status == "active":
        return item("asset_active", "pass", "native")
    if status == "retired":
        return item("asset_active", "violation", "native", f"asset_retired: {v.id}'s maintenance asset is retired")
    why = {"": "no maintenance asset status", "missing": "no maintenance asset for the vehicle",
           "unreachable": "maintenance product unreachable"}.get(status, f"unrecognised asset status {status!r}")
    return unverifiable("asset_active", f"{v.id}: {why}")


def vehicle_items(block: Block, v: Vehicle, facts: Facts, policy: AgencyPolicy) -> list[ClearanceItem]:
    n = block.needs
    out = [item("vehicle_ready", "pass" if v.status == "ready" else "violation", "native",
                "" if v.status == "ready" else f"{v.id} is {v.status}, not ready"),
           asset_active_item(v, facts)]
    if n.seats or need_unknown(block, "seats"):
        out.append(_capacity(block, v, facts, "vehicle_seats", "seats", "seats", lambda: v.seats < n.seats,
                             lambda: f"{v.id} has {v.seats} seats, needs {n.seats}", "seats"))
    if n.wheelchair_positions or need_unknown(block, "wheelchair_positions"):
        out.append(_capacity(block, v, facts, "vehicle_wheelchair", "wheelchair_positions", "wheelchair_positions",
                             lambda: v.wheelchair_positions < n.wheelchair_positions,
                             lambda: f"{v.id} has {v.wheelchair_positions} wheelchair positions, "
                                     f"needs {n.wheelchair_positions}", "wheelchair positions"))
    if n.lift or need_unknown(block, "lift"):
        out.append(_capacity(block, v, facts, "vehicle_lift", "lift_ok", "lift", lambda: not v.lift_ok,
                             lambda: f"{v.id} lift not operational", "the lift"))
    for extra in (vehicle_fit_item(block, v, facts, policy), vehicle_length_item(block, v, facts)):
        if extra is not None:
            out.append(extra)
    return out


# ---- operator rules -------------------------------------------------------------------------------
def _expired(valid_to: datetime, block: Block) -> bool:
    """A credential whose valid_to falls on day D is not valid for work on D or later (Review Focus 1:
    a violation on the day it expires, not the day after; A69). Dates are taken in valid_to's own zone."""
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
        return unverifiable("operator_lift_training", f"{op.id}: lift training (or the block's lift need) not known")
    return item("operator_lift_training", "pass" if op.lift_trained else "violation", basis,
                "" if op.lift_trained else
                f"lift_qualification: operator {op.id} is not lift-trained but block needs a lift")


def _hours_items(block: Block, op: Operator, facts: Facts, policy: AgencyPolicy) -> list[ClearanceItem]:
    lim, bh = policy.limits, block_hours(block)
    today_b = facts.record_basis("workforce", "hours_today", op)
    week_b = facts.record_basis("workforce", "hours_week", op)
    out: list[ClearanceItem] = []
    if today_b == "unknown":
        out.append(unverifiable("operator_hours_day", f"{op.id}: hours worked today not known"))
    else:
        out.append(day_hours_item(op.id, op.hours_today, bh, lim.max_operator_hours_day, today_b))
    cap = lim.max_operator_hours_week
    if week_b != "unknown":
        total = op.hours_week + bh
        bad = total > cap + EPS
        out.append(item("operator_hours_week", "violation" if bad else "pass", week_b,
                        f"operator_hours_week: {op.id} {op.hours_week:.1f}h in the 8-day window + {bh:.1f}h block "
                        f"= {total:.1f}h > max {cap:g}h" if bad else ""))
    elif today_b != "unknown" and op.hours_today + bh > cap + EPS:   # derived lower bound (A71)
        out.append(item("operator_hours_week", "violation", "derived",
                        f"operator_hours_week: {op.id} weekly hours not supplied, but today's {op.hours_today:.1f}h "
                        f"+ {bh:.1f}h block already exceeds max {cap:g}h (lower bound)"))
    else:
        out.append(unverifiable("operator_hours_week", f"{op.id}: hours in the 8-day window not known"))
    return out


def _lift_basis(block: Block, op: Operator, facts: Facts) -> Basis:
    return "unknown" if need_unknown(block, "lift") else facts.record_basis("workforce", "lift_trained", op)


def _qualification_items(block: Block, op: Operator, vehicle_type: str | None, facts: Facts) -> list[ClearanceItem]:
    """Per field (F2): the new model is used whenever ANY of its fields is supplied for this operator; each of
    licence class, endorsement, type training and expiry is then judged from its own data. The legacy list is
    used only when NONE of the new fields is supplied."""
    rb = lambda f: facts.record_basis("workforce", f, op)
    n = block.needs
    out: list[ClearanceItem] = []
    if vehicle_type is None and any(rb(f) != "unknown" for f in NEW_QUALIFICATION_FIELDS):
        # R2-1 / R3-1: the vehicle's type is not known (not supplied, blank, or not chosen yet). Only the checks
        # that depend on the type are unverifiable; everything else is judged from native data (A136)
        out += _new_model_items(block, op, None, None, facts)
    elif vehicle_type is None:
        # the legacy list only names types: without the vehicle's type it says nothing (never judged on a placeholder)
        out.append(unverifiable("operator_qualification",
                                f"{op.id}: the vehicle's type is not known, so the legacy qualification list "
                                "cannot be checked"))
    elif any(rb(f) != "unknown" for f in NEW_QUALIFICATION_FIELDS):        # new qualification model (D-1)
        req = facts.tables.vehicle_requirements.get(vehicle_type)
        if req is None:
            for rule in ("operator_licence_class", "operator_endorsement", "operator_vehicle_training"):
                out.append(unverifiable(rule, f"{op.id}: no licence requirement on record for vehicle type {vehicle_type}"))
        else:
            out += _new_model_items(block, op, vehicle_type, req, facts)
    elif rb("qualifications") != "unknown":                               # legacy list only
        ok = vehicle_type in op.qualifications
        out.append(item("operator_qualification", "pass" if ok else "violation", rb("qualifications"),
                        "" if ok else f"operator_qualification: {op.id} lacks {vehicle_type}"))
    else:
        out.append(unverifiable("operator_qualification",
                                f"{op.id}: no qualification data on record (licence, endorsement or training)"))
    if n.lift or need_unknown(block, "lift"):
        out.append(lift_training_item(op, _lift_basis(block, op, facts)))
    return out


def _passenger_endorsement_need(vtype: str | None, req, facts: Facts) -> bool | None:
    """Whether the passenger endorsement is needed. With the type unknown it is needed when EVERY configured type
    needs it, not needed when none does, and unknown (None) when the types disagree (R3-1, A136: computed from
    licence_classes.yaml, never hard-coded)."""
    if vtype is not None:
        return bool(req.get("passenger_endorsement"))
    flags = {bool(r.get("passenger_endorsement")) for r in facts.tables.vehicle_requirements.values()}
    return flags.pop() if len(flags) == 1 else None


def _new_model_items(block: Block, op: Operator, vtype: str | None, req, facts: Facts) -> list[ClearanceItem]:
    """`vtype` None = the vehicle's type is unknown: the licence class for the type, the trained-type membership and
    the expiry entry keyed by the type are unverifiable; the passenger endorsement (when every type needs it), lift
    training and every other expiry are still judged (R3-1)."""
    rb = lambda f: facts.record_basis("workforce", f, op)
    out: list[ClearanceItem] = []
    order = facts.tables.licence_order
    lb = rb("licence_class")
    no_type = f"{op.id}: the vehicle's type is not known"
    if lb == "unknown" or op.licence_class is None:
        out.append(unverifiable("operator_licence_class", f"{op.id}: licence class not on record"))
    elif vtype is None:
        out.append(unverifiable("operator_licence_class", f"{no_type}, so the licence class it needs is unknown"))
    else:
        need = req["min_licence_class"]
        ok = order.index(op.licence_class) >= order.index(need)
        out.append(item("operator_licence_class", "pass" if ok else "violation", lb,
                        "" if ok else f"{op.id}: licence class {op.licence_class} below {need} needed for {vtype}"))
    code = facts.tables.passenger_endorsement_code
    p_need = _passenger_endorsement_need(vtype, req, facts)
    needs_p = p_need is not False
    eb, tb, vb = rb("endorsements"), rb("trained_vehicle_types"), rb("training_valid_to")
    held_p = code in op.endorsements
    if p_need is None:
        out.append(unverifiable("operator_endorsement",
                                f"{no_type}, and the configured types disagree on the passenger endorsement"))
    elif needs_p:
        if eb == "unknown":
            out.append(unverifiable("operator_endorsement", f"{op.id}: endorsements not on record"))
        else:
            out.append(item("operator_endorsement", "pass" if held_p else "violation", eb,
                            "" if held_p else f"{op.id}: passenger endorsement {code} missing"))
    train = req["training_type"] if req is not None else None
    held_t = train in op.trained_vehicle_types
    if tb == "unknown":
        out.append(unverifiable("operator_vehicle_training", f"{op.id}: trained vehicle types not on record"))
    elif train is None:
        out.append(unverifiable("operator_vehicle_training", f"{no_type}, so the type training it needs is unknown"))
    else:
        out.append(item("operator_vehicle_training", "pass" if held_t else "violation", tb,
                        "" if held_t else f"{op.id}: not trained on vehicle type {train}"))
    # expiry of every credential this block needs that the operator is KNOWN to hold; a credential whose holding
    # is unknown makes the expiry check unverifiable (never a silent pass)
    held: list[str] = []
    unsure: list[str] = []
    bases: list[Basis] = [vb]
    if p_need is None:
        unsure.append(code)
    if train is None:
        unsure.append("training for the vehicle's type")
    for cred, needed, basis, has in ((code, p_need is True, eb, held_p), (train, train is not None, tb, held_t)):
        if not needed:
            continue
        bases.append(basis)
        if basis == "unknown":
            unsure.append(cred)
        elif has:
            held.append(cred)
    if block.needs.lift or need_unknown(block, "lift"):
        lb2 = _lift_basis(block, op, facts)
        bases.append(lb2)
        if lb2 == "unknown":
            unsure.append("lift")
        elif op.lift_trained:
            held.append("lift")
    if held or unsure:
        out.append(_expiry_item(block, op, held, unsure, vb, weakest(*bases)))
    return out


def _expiry_item(block: Block, op: Operator, held: list[str], unsure: list[str], dates_basis: Basis,
                 basis: Basis) -> ClearanceItem:
    if held and dates_basis != "unknown":
        expired = [c for c in held if c in op.training_valid_to and _expired(op.training_valid_to[c], block)]
        if expired:   # a KNOWN held credential, KNOWN expired: a violation whatever else is unknown
            return item("operator_training_expiry", "violation", dates_basis,
                        f"{op.id}: {', '.join(expired)} expired on or before the block's day")
    if dates_basis == "unknown":
        return unverifiable("operator_training_expiry", f"{op.id}: expiry dates not on record")
    undated = [c for c in held if c not in op.training_valid_to]
    if undated or unsure:
        why = [f"no expiry date on record for {', '.join(undated)}"] if undated else []
        why += [f"not known whether {', '.join(unsure)} is held"] if unsure else []
        return unverifiable("operator_training_expiry", f"{op.id}: " + "; ".join(why))
    return item("operator_training_expiry", "pass", basis)


def known_vehicle_type(v: Vehicle, facts: Facts) -> str | None:
    """The vehicle's type for the OPERATOR rules: None when the product does not supply it or this record's
    type was blank (the adapters' placeholder must never be judged; R2-1)."""
    return None if facts.record_basis("scheduling", "type", v) == "unknown" else v.type


def known_need_type(block: Block) -> str | None:
    """The block's required type for a candidate PRE-filter (vehicle not chosen yet): None when the block did not
    declare it (C-2), so a placeholder is never judged (R2-1)."""
    return None if need_unknown(block, "type") else block.needs.type


def operator_items(block: Block, op: Operator, vehicle_type: str | None, facts: Facts,
                   policy: AgencyPolicy) -> list[ClearanceItem]:
    """All operator rules for one block. `vehicle_type` is the KNOWN type of the vehicle that will run it (use
    `known_vehicle_type`). None (not known, or not chosen yet) makes every type-dependent qualification rule
    unverifiable (R2-1); candidate pre-filters then keep the operator and the option level re-judges it."""
    rb = lambda f, fields=None: facts.record_basis("workforce", f, op, fields)
    lim = policy.limits
    out = _hours_items(block, op, facts, policy)
    out += _qualification_items(block, op, vehicle_type, facts)
    ab = rb("availability")
    if ab == "unknown":
        out.append(unverifiable("operator_availability", f"{op.id}: availability window not known"))
    else:   # a bound the product holds as empty is open (A126); a blank cell is listed as unknown (F3)
        bad = (op.available_from is not None and op.available_from > block.start) or \
              (op.available_to is not None and op.available_to < block.end)
        out.append(item("operator_availability", "violation" if bad else "pass", ab,
                        f"operator_not_available: {op.id} outside the block window" if bad else ""))
    # medical card: a LEGAL rule (F8, A134): known expired = violation, unknown = unverifiable
    mb = rb("medical_valid_to")
    if mb == "unknown" or op.medical_valid_to is None:
        out.append(unverifiable("operator_medical", f"{op.id}: no medical card date on record"))
    else:
        bad = _expired(op.medical_valid_to, block)
        out.append(item("operator_medical", "violation" if bad else "pass", mb,
                        f"{op.id}: medical card expired" if bad else ""))
    # optional rules (A72): not_evaluated without data; a known breach warns and needs acknowledgement
    rest_b = rb("duty_times", ("last_duty_end",))
    if rest_b == "unknown" or op.last_duty_end is None:
        out.append(item("operator_rest", "not_evaluated", "unknown", f"{op.id}: previous duty end not on record"))
    else:
        rest = (block.start - op.last_duty_end).total_seconds() / 3600.0
        short = rest < lim.min_rest_hours - EPS
        out.append(item("operator_rest", "warn" if short else "pass", rest_b,
                        f"{op.id}: {rest:.1f}h rest before the block, minimum {lim.min_rest_hours:g}h" if short else "",
                        short))
    spread_b = rb("duty_times", ("duty_start_today",))
    if spread_b == "unknown" or op.duty_start_today is None:
        out.append(item("operator_spread", "not_evaluated", "unknown", f"{op.id}: duty start not on record"))
    else:
        spread = (block.end - op.duty_start_today).total_seconds() / 3600.0
        long = spread > lim.max_spread_hours + EPS
        out.append(item("operator_spread", "warn" if long else "pass", spread_b,
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


# Search cap for the exact minimum (F6, A127). The agency fleets have at most 8 affected blocks with a handful of
# candidates each, far below this; beyond it the greedy upper bound is used and flagged as not exact.
MAX_SPARE_SEARCH_NODES = 200_000


@dataclass(frozen=True)
class SpareCount:
    """Fewest distinct spares that cover all blocks. `n` None = some block has no candidate (or no assignment
    exists at all); `exact` False = the search cap was hit and `n` is the greedy UPPER bound (fail closed: it can
    only overstate what is needed)."""
    n: int | None
    exact: bool = True
    assignment: tuple[tuple[str, str], ...] = ()   # (block id, spare id) of one minimum cover (when exact)


def _greedy_spares(order: Sequence[Block], candidates: dict[str, list[str]]) -> int | None:
    used: dict[str, list[tuple[datetime, datetime]]] = {}
    for b in order:
        cands = candidates.get(b.id, [])
        pick = next((s for s in cands if s in used
                     and not any(overlaps(b.start, b.end, s0, e0) for s0, e0 in used[s])), None)
        if pick is None:
            pick = next((s for s in cands if s not in used), None)
        if pick is None:
            return None
        used.setdefault(pick, []).append((b.start, b.end))
    return len(used)


def count_distinct_spares(blocks: Sequence[Block], candidates: dict[str, list[str]],
                          max_nodes: int = MAX_SPARE_SEARCH_NODES) -> SpareCount:
    """Exact minimum number of distinct spares when a spare may serve several non-overlapping blocks (Review
    Focus 7, F6): depth-first branch and bound over blocks in start order, reusing a compatible spare before
    opening a new one, pruning any branch that cannot beat the best found. If the search exceeds `max_nodes`
    the greedy result is returned as an upper bound with `exact=False`."""
    order = sorted(blocks, key=lambda x: (x.start, x.id))
    if any(not candidates.get(b.id) for b in order):
        return SpareCount(None)
    if not order:
        return SpareCount(0)
    greedy = _greedy_spares(order, candidates)
    best: list = [len(order) + 1, ()]      # strictly better than "no cover"; the greedy count is beaten or matched
    nodes = [0]
    used: dict[str, list[tuple[datetime, datetime]]] = {}
    path: list[tuple[str, str]] = []

    def search(i: int) -> bool:   # False = cap exceeded
        nodes[0] += 1
        if nodes[0] > max_nodes:
            return False
        if len(used) >= best[0]:
            return True
        if i == len(order):
            best[0], best[1] = len(used), tuple(path)
            return True
        b = order[i]
        cands = candidates[b.id]
        reuse = [s for s in cands if s in used and not any(overlaps(b.start, b.end, s0, e0) for s0, e0 in used[s])]
        fresh = [s for s in cands if s not in used]
        for s in reuse + fresh:
            used.setdefault(s, []).append((b.start, b.end))
            path.append((b.id, s))
            ok = search(i + 1)
            path.pop()
            used[s].pop()
            if not used[s]:
                del used[s]
            if not ok:
                return False
        return True

    if not search(0):
        return SpareCount(greedy, exact=False)
    return SpareCount(best[0], True, best[1]) if best[0] <= len(order) else SpareCount(None)


def min_distinct_spares(blocks: list[Block], candidates: dict[str, list[str]]) -> int | None:
    """Fewest distinct spares (exact, see `count_distinct_spares`; the greedy upper bound past the search cap).
    None when some block has no candidate."""
    return count_distinct_spares(blocks, candidates).n
