# ruff: noqa: B008
"""SIMULATED workforce product: daily CSV export for reads (A3), flaky operator-assignment write.
It does NOT enforce hours/qualification on writes: hard limits live in tools code, not here."""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Response

from .chaos import Chaos
from .clock import SimClock, iso, parse_ts
from .common import Ctx, make_app
from .seed import build_fleet

COLUMNS = ["operator", "qualifications", "lift_trained", "hours_today", "hours_week",
           "available_from", "available_to", "on_extra_board"]


def create_app(clock: SimClock, chaos: Chaos, db_path: Any = None) -> FastAPI:
    ctx = Ctx("workforce", clock, chaos, db_path)
    st = ctx.store
    tenant = Depends(ctx.tenant)

    def seeder(a: str) -> None:
        fleet = build_fleet(a, clock.now())
        for o in fleet.operators:
            st.put(a, "operators", o["native"], {
                "operator": o["native"], "qualifications": o["qualifications"], "lift_trained": o["lift_trained"],
                "hours_today": o["hours_today"], "hours_week": o["hours_week"],
                "available_from": o["available_from"], "available_to": o["available_to"],
                "on_extra_board": o["on_extra_board"],
            })
        for b in fleet.blocks:
            st.put(a, "assignments", b["id"], {"block": b["id"], "operator": b["operator"]})

    app = make_app(ctx, "workforce", seeder)

    def op(a: str, oid: str) -> dict:
        o = st.get(a, "operators", oid)
        if o is None:
            raise HTTPException(404, "operator not found")
        return o

    def rendered(o: dict) -> dict:
        """Daily export: availability window as RFC3339 UTC on the current sim day (MOCK_API: all
        timestamps RFC3339). Stored internally as HH:MM; full timestamps pass through unchanged."""
        day = clock.now().strftime("%Y-%m-%d")
        out = dict(o)
        for k in ("available_from", "available_to"):
            v = out.get(k)
            if isinstance(v, str) and len(v) == 5 and v[2] == ":":
                out[k] = f"{day}T{v}:00Z"
        return out

    @app.get("/export/operators.csv")
    def export(a: str = tenant):
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=COLUMNS, lineterminator="\n")
        w.writeheader()
        for o in st.all(a, "operators"):
            row = rendered(o)
            row["lift_trained"] = str(row["lift_trained"]).lower()
            row["on_extra_board"] = str(row["on_extra_board"]).lower()
            w.writerow(row)
        return Response(buf.getvalue(), media_type="text/csv")

    @app.get("/operators/{oid}")
    def get_operator(oid: str, a: str = tenant):
        return rendered(op(a, oid))

    @app.get("/assignments/{bid}")
    def get_assignment(bid: str, a: str = tenant):
        d = st.get(a, "assignments", bid)
        if d is None:
            raise HTTPException(404, "block not found")
        return d

    @app.post("/blocks/{bid}/operator")
    def assign(bid: str, body: dict = Body(...), a: str = tenant):
        ctx.write_gate()
        cur = st.get(a, "assignments", bid)
        if cur is None:
            raise HTTPException(404, "block not found")
        new = op(a, body.get("operator", ""))["operator"]
        prev = cur["operator"]
        st.put(a, "assignments", bid, {"block": bid, "operator": new})
        return {"block": bid, "operator": new, "previous": prev}

    @app.get("/availability-changes")
    def changes(since: str | None = None, a: str = tenant):
        return [c for c in st.all(a, "changes") if not since or parse_ts(c["changed_at"]) >= parse_ts(since)]

    @app.post("/sim/operator")
    def sim_operator(body: dict = Body(...), x_agency: str = Header()):
        """Update operator fields and/or log an availability change (e.g. status=unavailable)."""
        o = op(x_agency, body["operator"])
        for k in COLUMNS[1:]:
            if k in body:
                o[k] = body[k]
        st.put(x_agency, "operators", o["operator"], o)
        if "status" in body:
            n = st.next_seq(x_agency, "change")
            st.put(x_agency, "changes", f"{n:08d}",
                   {"operator": o["operator"], "status": body["status"], "changed_at": iso(clock.now())})
        return o

    return app
