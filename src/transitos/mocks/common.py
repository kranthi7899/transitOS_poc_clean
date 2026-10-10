# ruff: noqa: B008
"""Shared plumbing for the five mock apps: tenancy, chaos guard, /sim admin routes."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import Body, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse

from .chaos import MODES, Chaos
from .clock import SimClock
from .seed import known_agency
from .store import Store


class Ctx:
    def __init__(self, product: str, clock: SimClock, chaos: Chaos, db_path: Any) -> None:
        self.product, self.clock, self.chaos = product, clock, chaos
        self.store = Store(db_path)

    def tenant(self, x_agency: str | None = Header(default=None)) -> str:
        """FastAPI dependency for every non-/sim route: tenancy + chaos guard."""
        if not x_agency:
            raise HTTPException(400, "X-Agency header required")
        if self.chaos.on("timeout"):
            raise HTTPException(504, "upstream timeout (chaos)")
        if self.product == "maintenance" and self.chaos.on("http_429"):
            raise HTTPException(429, "rate limited (chaos)", headers={"Retry-After": "1"})
        return x_agency

    def write_gate(self) -> None:
        if self.chaos.on("reject_write"):
            raise HTTPException(503, "write rejected (chaos)")


def make_app(
    ctx: Ctx, title: str, seeder: Callable[[str], None], on_reset: Callable[[str | None], None] | None = None
) -> FastAPI:
    app = FastAPI(title=f"SIMULATED {title} mock")
    app.state.ctx = ctx

    def agency_of(x_agency: str | None) -> str:
        if not x_agency:
            raise HTTPException(400, "X-Agency header required")
        return x_agency

    @app.post("/sim/reset")
    def reset(x_agency: str | None = Header(default=None)):
        ctx.store.clear(x_agency)
        if x_agency is None:
            ctx.chaos.clear()
        if on_reset:
            on_reset(x_agency)
        return {"ok": True}

    @app.post("/sim/seed")
    def seed(x_agency: str | None = Header(default=None)):
        a = agency_of(x_agency)
        if not known_agency(a):
            raise HTTPException(404, f"unknown agency {a}")
        ctx.store.clear(a)
        seeder(a)
        return {"ok": True, "agency": a}

    @app.post("/sim/chaos")
    def chaos(body: dict = Body(...)):
        mode, on = body.get("mode"), bool(body.get("on", True))
        if mode not in MODES:
            raise HTTPException(422, f"mode must be one of {MODES}")
        ctx.chaos.set(mode, on)
        return {"active": ctx.chaos.active()}

    @app.get("/sim/clock")
    def clock_get():
        return {"now": ctx.clock.iso()}

    @app.post("/sim/advance")
    def advance(body: dict = Body(default={})):
        ctx.clock.advance(hours=float(body.get("hours", 0)), minutes=float(body.get("minutes", 0)))
        return {"now": ctx.clock.iso()}

    @app.exception_handler(HTTPException)
    async def _h(_, exc: HTTPException):  # keep {"detail":..} and headers
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)

    return app
