"""The golden table (golden_clearance.yaml) is written from the policy text; this driver only translates its tiny
DSL into clearance inputs and compares verdicts. If a row and the code disagree, the code is wrong until the
policy text says otherwise."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from transitos.clearance import Option, evaluate_option
from transitos.clearance.evaluate import (
    Assign,
    kill_switch_item,
    operator_items,
    vehicle_items,
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


def block(spec, bid="b1", op="op-1", start_h=1.0, hours=4.0) -> Block:
    spec = dict(spec or {})
    start_h, hours = spec.pop("start_h", start_h), spec.pop("hours", hours)
    needs = {"seats": 30, "wheelchair_positions": 1, "lift": True, "type": "standard", **spec}
    return Block(id=bid, vehicle_id="veh-1", operator_id=op, start=T0 + timedelta(hours=start_h),
                 end=T0 + timedelta(hours=start_h + hours), start_depot="North", needs=Needs(**needs))


def vehicle(spec, vid="veh-2") -> Vehicle:
    base = {"type": "standard", "seats": 40, "wheelchair_positions": 2, "lift_ok": True, "status": "ready"}
    return Vehicle(id=vid, agency="a", depot="North", **{**base, **(spec or {})})


def operator(spec, oid="op-1", expires=None, window=None) -> Operator:
    far = datetime(2030, 1, 1, tzinfo=UTC)
    base = {"licence_class": "B", "endorsements": ["P"], "trained_vehicle_types": ["standard"],
            "training_valid_to": {"P": far, "standard": far, "lift": far}, "lift_trained": True,
            "hours_today": 4.0, "hours_week": 20.0}
    spec = {k: _date(v) for k, v in (spec or {}).items()}
    if "last_duty_end_h" in spec:
        spec["last_duty_end"] = T0 + timedelta(hours=spec.pop("last_duty_end_h"))
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
        ops = {x: operator(o.get("operator"), x) for x in ("op-1", "op-2")}
        com = o.get("commitments", {})
        commitments = None if com is None else {
            k: [(T0 + timedelta(hours=s), T0 + timedelta(hours=e), bid) for s, e, bid in v] for k, v in com.items()}
        f = Facts(blocks=blocks, vehicles={v: vehicle(None, v) for v in o["spares"]}, operators=ops,
                  ready_count=o["ready"], commitments=commitments, profiles=profiles(None, False))
        opt = Option(tuple(Assign(a[0], a[1], a[2] if len(a) > 2 else None) for a in o["assign"]))
        c = evaluate_option(opt, f, pol, T0)
        return c.items, c
    b = block(g.get("block"))
    v = vehicle(g.get("vehicle"))
    op = operator(g.get("operator"), expires=g.get("expires"), window=g.get("operator_window_h"))
    f = Facts(blocks={b.id: b}, vehicles={v.id: v}, operators={op.id: op}, ready_count=3, commitments={},
              profiles=profiles(g.get("drop"), g.get("legacy")))
    items = vehicle_items(b, v, f, pol) + operator_items(b, op, v.type, f, pol)
    return items, None


@pytest.mark.parametrize("row", ROWS, ids=[r["id"] for r in ROWS])
def test_golden_table_matches_policy_text(row):
    items, c = run_row(row)
    exp = row["expect"]
    if "excluded" in exp:
        assert c is not None and c.excluded == exp["excluded"], [i.model_dump() for i in c.items]
        return
    got = [i for i in items if i.rule == row["rule"]]
    assert got, (row["rule"], [i.model_dump() for i in items])
    sev = ["pass", "not_evaluated", "warn", "unverifiable", "violation"]
    it = max(got, key=lambda i: sev.index(i.status))      # per-block repeats: the worst verdict stands
    for k, want in exp.items():
        assert getattr(it, k) == want, (row["id"], row["source"], it.model_dump())


def test_golden_table_covers_every_status_and_basis_and_cites_its_source():
    assert all(r.get("source") for r in ROWS) and len({r["id"] for r in ROWS}) == len(ROWS)
    seen = [(r["expect"].get("status"), r["expect"].get("basis")) for r in ROWS if "status" in r["expect"]]
    assert {s for s, _ in seen} == {"pass", "warn", "unverifiable", "not_evaluated", "violation"}
    assert {b for _, b in seen if b} == {"native", "derived", "assumed", "unknown"}


def test_no_golden_row_lets_missing_data_become_a_violation_or_a_pass():
    for r in ROWS:
        if r["expect"].get("basis") == "unknown":
            assert r["expect"]["status"] in ("unverifiable", "not_evaluated") or r["rule"] in (
                "double_booking",), r["id"]    # double-booking is the one rule that fails closed
