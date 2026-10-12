"""The golden table (golden_clearance.yaml) is written from the policy text; this driver only translates its tiny
DSL into clearance inputs and compares verdicts. If a row and the code disagree, the code is wrong until the
policy text says otherwise."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from transitos.clearance import Option, safe_evaluate
from transitos.clearance.evaluate import (
    Assign,
    kill_switch_item,
)
from transitos.clearance.facts import SCHEDULING_FIELDS, Facts, native_profile
from transitos.contracts.objects import Block, Needs, Operator, Vehicle
from transitos.contracts.policy import AgencyPolicy, Limits

ROWS = yaml.safe_load((Path(__file__).parent / "golden_clearance.yaml").read_text())
T0 = datetime(2026, 10, 10, 6, 0, tzinfo=UTC)
NEW_QUAL = ("licence_class", "endorsements", "trained_vehicle_types", "training_valid_to")


def _date(v):
    if isinstance(v, str) and len(v) == 10:
        return datetime.fromisoformat(v).replace(tzinfo=UTC)
    return v


def block(spec, bid="b1", op="op-1", start_h=1.0, hours=4.0, unknown=()) -> Block:
    spec = dict(spec or {})
    start_h, hours = spec.pop("start_h", start_h), spec.pop("hours", hours)
    needs = {"seats": 30, "wheelchair_positions": 1, "lift": True, "type": "standard", **spec,
             "unknown_fields": list(unknown or ())}
    return Block(id=bid, vehicle_id="veh-1", operator_id=op, start=T0 + timedelta(hours=start_h),
                 end=T0 + timedelta(hours=start_h + hours), start_depot="North", needs=Needs(**needs))


def vehicle(spec, vid="veh-2", unknown=()) -> Vehicle:
    base = {"type": "standard", "seats": 40, "wheelchair_positions": 2, "lift_ok": True, "status": "ready"}
    return Vehicle(id=vid, agency="a", depot="North", unknown_fields=list(unknown or ()), **{**base, **(spec or {})})


def operator(spec, oid="op-1", expires=None, window=None, unknown=()) -> Operator:
    far = datetime(2030, 1, 1, tzinfo=UTC)
    base = {"licence_class": "B", "endorsements": ["P"], "trained_vehicle_types": ["standard"],
            "training_valid_to": {"P": far, "standard": far, "lift": far}, "lift_trained": True,
            "hours_today": 4.0, "hours_week": 20.0, "medical_valid_to": far,
            "unknown_fields": list(unknown or ())}
    spec = {k: _date(v) for k, v in (spec or {}).items()}
    for key in ("last_duty_end", "duty_start_today"):
        if f"{key}_h" in spec:
            spec[key] = T0 + timedelta(hours=spec.pop(f"{key}_h"))
    base.update(spec)
    for cred, d in (expires or {}).items():
        base["training_valid_to"] = {**base["training_valid_to"], cred: _date(d)}
    if window:
        base["available_from"], base["available_to"] = T0 + timedelta(hours=window[0]), T0 + timedelta(hours=window[1])
    return Operator(id=oid, **base)


def profiles(drop, legacy):
    drop = set(drop or [])
    if legacy:
        drop |= set(NEW_QUAL)
    return {"scheduling": native_profile("scheduling", drop=drop & set(SCHEDULING_FIELDS)),
            "workforce": native_profile("workforce", drop=drop - set(SCHEDULING_FIELDS))}


def policy(spec) -> AgencyPolicy:
    spec = dict(spec or {})
    lim = Limits(**spec.pop("limits", {}))
    return AgencyPolicy(agency="a", limits=lim, **spec)


def run_row(row):
    g = row.get("given", {})
    pol = policy(g.get("policy"))
    if "kill_switch" in row:
        return [kill_switch_item(row["kill_switch"])], None
    if "option" in row:
        o = row["option"]
        blocks = {b[0]: block(None, b[0], b[3] if len(b) > 3 else f"op-{n + 1}", b[1], b[2])
                  for n, b in enumerate(o["blocks"])}
        ids = {"op-1", "op-2"} | {b.operator_id for b in blocks.values() if b.operator_id} | \
              {a[2] for a in o["assign"] if len(a) > 2}
        per = o.get("operators", {})
        ops = {x: operator({**(o.get("operator") or {}), **per.get(x, {})}, x)
               for x in sorted(ids - set(o.get("absent_operators", [])))}
        com = o.get("commitments", {})
        commitments = None if com is None else {
            k: [(T0 + timedelta(hours=s), T0 + timedelta(hours=e), bid) for s, e, bid in v] for k, v in com.items()}
        f = Facts(blocks=blocks, vehicles={v: vehicle(None, v) for v in o["spares"]}, operators=ops,
                  ready_count=o["ready"], commitments=commitments, profiles=profiles(None, False),
                  asset_status={v: "active" for v in o["spares"]})   # S2 F4: rows that do not care: active
        opt = Option(tuple(Assign(a[0], a[1], a[2] if len(a) > 2 else None) for a in o["assign"]))
        c = safe_evaluate(opt, f, pol, T0)        # the entry point every caller uses (fails closed)
        return c.items, c
    b = block(g.get("block"), unknown=g.get("needs_unknown"))
    v = vehicle(g.get("vehicle"), unknown=g.get("vehicle_unknown"))
    op = operator(g.get("operator"), expires=g.get("expires"), window=g.get("operator_window_h"),
                  unknown=g.get("operator_unknown"))
    f = Facts(blocks={b.id: b}, vehicles={v.id: v}, operators={op.id: op}, ready_count=3, commitments={},
              profiles=profiles(g.get("drop"), g.get("legacy")), asset_status=asset_feed(g.get("asset_status"), v.id))
    # the planner's own path: one block onto one spare, the block keeps its operator (R2-1: the operator rules
    # must see the vehicle type exactly as the planner hands it over, not as this driver would)
    c = safe_evaluate(Option((Assign(b.id, v.id),)), f, pol, T0)
    return c.items, c


def asset_feed(status: str | None, vid: str) -> dict[str, str] | None:
    """S2 F4 DSL: default active; `missing` = no entry for the vehicle; `no_feed` = no asset-status read."""
    status = status or "active"
    if status == "no_feed":
        return None
    return {} if status == "missing" else {vid: status}


@pytest.mark.parametrize("row", ROWS, ids=[r["id"] for r in ROWS])
def test_golden_table_matches_policy_text(row):
    items, c = run_row(row)
    exp = row["expect"]
    if "excluded_single" in exp:
        assert c.excluded == exp["excluded_single"], [i.model_dump() for i in c.items]
        return
    if "excluded" in exp:
        assert c is not None and c.excluded == exp["excluded"], [i.model_dump() for i in c.items]
        return
    got = [i for i in items if i.rule == row["rule"]]
    if exp.get("absent"):
        assert not got, (row["id"], [i.model_dump() for i in got])
        return
    assert got, (row["rule"], [i.model_dump() for i in items])
    sev = ["pass", "not_evaluated", "warn", "unverifiable", "violation"]
    it = max(got, key=lambda i: sev.index(i.status))      # per-block repeats: the worst verdict stands
    for k, want in exp.items():
        assert getattr(it, k) == want, (row["id"], row["source"], it.model_dump())


# ---- coverage: rule x status x basis (F7) ------------------------------------------------------------------
STATUSES = ("pass", "warn", "unverifiable", "not_evaluated", "violation")
BASES = ("native", "derived", "assumed", "unknown")
SAFETY = ("vehicle_seats", "vehicle_wheelchair", "vehicle_lift", "operator_hours_day", "operator_licence_class",
          "operator_endorsement", "operator_vehicle_training", "operator_lift_training", "operator_training_expiry",
          "operator_qualification", "operator_availability", "operator_medical", "last_spare")
OPTIONAL = ("operator_rest", "operator_spread")
_KNOWN = (("pass", "native"), ("violation", "native"))
_UNV = (("unverifiable", "unknown"),)
# Every combination that CAN occur, from the classification (amendment A + plan "Rule classification" + rulings).
POSSIBLE: dict[str, tuple[tuple[str, str], ...]] = {
    **{r: _KNOWN + _UNV for r in SAFETY},
    "vehicle_ready": _KNOWN,
    "vehicle_fit": _KNOWN + (("warn", "native"),) + _UNV,
    "vehicle_length": _KNOWN + (("pass", "assumed"), ("violation", "assumed")) + _UNV,
    "operator_hours_week": _KNOWN + (("violation", "derived"),) + _UNV,
    **{r: (("pass", "native"), ("warn", "native"), ("not_evaluated", "unknown")) for r in OPTIONAL},
    "double_booking": _KNOWN + (("violation", "unknown"),),
    "kill_switch": _KNOWN,
    "operator_eligible": (("violation", "native"),),
    "clearance_error": (("violation", "unknown"),),
}


def why_impossible(rule: str, status: str, basis: str) -> str:
    """The reason a combination outside POSSIBLE cannot occur (the coverage test requires one for each)."""
    if status == "unverifiable" and basis != "unknown":
        return "unverifiable means the rule's data is unknown: its basis is unknown by definition"
    if status == "not_evaluated" and basis != "unknown":
        return "not_evaluated means there is no data: basis unknown by definition"
    if basis == "unknown" and status in ("pass", "warn"):
        return "A Q3: missing data is never a pass, and a warning needs a KNOWN breach"
    if basis == "unknown" and status == "violation":
        return "A Q3: missing data is never a violation (only double-booking and clearance errors fail closed)"
    if rule in ("vehicle_ready", "kill_switch", "operator_eligible", "clearance_error") and status in (
            "unverifiable", "not_evaluated"):
        return "no missing-data mode: status is mandatory (a record without one is rejected), the kill switch " \
               "and the eligible list are always known, an error is itself the verdict"
    if rule == "double_booking" and status in ("unverifiable", "not_evaluated"):
        return "P table: double-booking fails CLOSED without a schedule feed (violation, basis unknown)"
    if status == "not_evaluated":
        return "only the optional rules (rest, spread) are not_evaluated; safety/legal rules are unverifiable"
    if status == "unverifiable" and rule in OPTIONAL:
        return "P table: optional rules are not_evaluated without data, never unverifiable"
    if status == "warn":
        return "only the graded vehicle fit and the optional rules warn; hard rules exclude"
    if rule in OPTIONAL and status == "violation":
        return "A72: a known rest/spread breach warns (acknowledgement), it does not exclude"
    if basis == "derived":
        return "only the weekly cap has a derived source (today's hours as a lower bound), which can only prove " \
               "a breach, never a pass"
    if basis == "assumed":
        return "only the vehicle length has an agency default (A66 typical length by class)"
    if rule == "operator_eligible":
        return "the rule only exists to refuse an operator outside the code-built list"
    if rule == "clearance_error":
        return "an error is always a fail-closed violation"
    return ""


def actual_combos() -> dict[tuple[str, str, str], str]:
    out: dict[tuple[str, str, str], str] = {}
    sev = ["pass", "not_evaluated", "warn", "unverifiable", "violation"]
    for r in ROWS:
        if "rule" not in r or r["expect"].get("absent") or "excluded" in r["expect"]:
            continue
        items, _ = run_row(r)
        it = max((i for i in items if i.rule == r["rule"]), key=lambda i: sev.index(i.status))
        out.setdefault((it.rule, it.status, it.basis), r["id"])
    return out


def test_golden_table_cites_its_source_and_has_unique_ids():
    assert all(r.get("source") for r in ROWS) and len({r["id"] for r in ROWS}) == len(ROWS)


def test_golden_table_covers_every_possible_rule_status_basis_combination():
    covered = actual_combos()
    possible = {(r, s, b) for r, combos in POSSIBLE.items() for s, b in combos}
    assert set(covered) - possible == set(), "a row produced a combination the classification says cannot occur"
    assert possible - set(covered) == set(), "a possible combination has no golden row"


def test_every_combination_outside_the_classification_has_a_stated_reason():
    for rule, combos in POSSIBLE.items():
        for s in STATUSES:
            for b in BASES:
                if (s, b) not in combos:
                    assert why_impossible(rule, s, b), (rule, s, b)
