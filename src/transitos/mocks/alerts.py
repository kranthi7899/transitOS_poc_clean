# ruff: noqa: B008
"""SIMULATED rider-alert product (GTFS-RT service-alert style). Eventually visible on the sim clock.
No idempotency: posting twice creates two alerts. A correction supersedes the original once visible."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import Body, Depends, FastAPI, Header, HTTPException

from .chaos import Chaos
from .clock import SimClock, iso, parse_ts
from .common import Ctx, make_app

DEFAULT_DELAY_MIN = 5
LATE_FACTOR = 6


def create_app(clock: SimClock, chaos: Chaos, db_path: Any = None) -> FastAPI:
    ctx = Ctx("alerts", clock, chaos, db_path)
    st = ctx.store
    tenant = Depends(ctx.tenant)
    app = make_app(ctx, "alerts", lambda a: None)

    def delay(a: str) -> int:
        d = (st.get(a, "config", "cfg") or {}).get("visibility_delay_min", DEFAULT_DELAY_MIN)
        return d * (LATE_FACTOR if chaos.on("late") else 1)

    def create(a: str, block: str, header: str, desc: str, corrects: str | None = None) -> dict:
        n = st.next_seq(a, "alert")
        now = clock.now()
        al = {
            "id": f"alert-{n}", "block": block, "header_text": header, "description_text": desc,
            "created_at": iso(now), "visible_after": iso(now + timedelta(minutes=delay(a))),
            "corrects": corrects,
        }
        st.put(a, "alerts", al["id"], al)
        return al

    def visible(a: str) -> list[dict]:
        now = clock.now()
        vis = [x for x in st.all(a, "alerts") if parse_ts(x["visible_after"]) <= now]
        superseded = {x["corrects"] for x in vis if x["corrects"]}
        return [x for x in vis if x["id"] not in superseded]

    @app.post("/alerts")
    def post_alert(body: dict = Body(...), a: str = tenant):
        if not body.get("block"):
            raise HTTPException(422, "block required")
        return {"id": create(a, body["block"], body.get("header_text", ""), body.get("description_text", ""))["id"]}

    @app.get("/alerts")
    def list_alerts(block: str | None = None, a: str = tenant):
        return [x for x in visible(a) if not block or x["block"] == block]

    @app.post("/alerts/{aid}/correction")
    def correction(aid: str, body: dict = Body(...), a: str = tenant):
        orig = st.get(a, "alerts", aid)
        if orig is None:
            raise HTTPException(404, "alert not found")
        new = create(a, orig["block"], body.get("header_text", orig["header_text"]),
                     body.get("description_text", orig["description_text"]), corrects=aid)
        return {"id": new["id"]}

    @app.post("/sim/config")
    def sim_config(body: dict = Body(...), x_agency: str = Header()):
        c = st.get(x_agency, "config", "cfg") or {}
        c.update({k: body[k] for k in ("visibility_delay_min",) if k in body})
        st.put(x_agency, "config", "cfg", c)
        return c

    return app
