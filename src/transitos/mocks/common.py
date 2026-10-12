# ruff: noqa: B008
"""Shared plumbing for the five mock apps: tenancy, chaos guard, /sim admin routes."""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Body, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse

from .chaos import MODES, Chaos
from .clock import SimClock, parse_ts
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

    # ---- Phase 10 Step 4.2 (A108, A110): wire time format, render-only, default OFF ----------------------------
    def time_format(self, agency: str) -> dict:
        return self.store.get(agency, "config", "time_format") or dict(TIME_FORMAT_OFF)

    def local_zone(self, agency: str) -> ZoneInfo | None:
        tz = self.time_format(agency).get("local_tz")
        return ZoneInfo(tz) if tz else None

    def out_ts(self, agency: str, value: Any) -> Any:
        """A stored UTC timestamp as this product renders it: zone-less local wall time when the agency's knob is
        on, else unchanged. The store always keeps UTC."""
        zi = self.local_zone(agency)
        if zi is None or not isinstance(value, str) or not value:
            return value
        return parse_ts(value).astimezone(zi).strftime("%Y-%m-%dT%H:%M:%S")

    def out_offset(self, agency: str, value: str) -> str:
        zi = self.local_zone(agency)
        off = parse_ts(value).astimezone(zi).utcoffset() if zi else None
        mins = int(off.total_seconds() // 60) if off is not None else 0
        return f"{'-' if mins < 0 else '+'}{abs(mins) // 60:02d}:{abs(mins) % 60:02d}"

    def out_doc(self, agency: str, doc: dict, keys: tuple[str, ...]) -> dict:
        """Render the timestamp `keys` of one record; with `utc_offset` on, add {key: "+HH:MM"} for each."""
        fmt = self.time_format(agency)
        if not fmt.get("local_tz"):
            return doc
        out = dict(doc)
        offsets = {}
        for k in keys:
            if isinstance(doc.get(k), str) and doc[k]:
                out[k] = self.out_ts(agency, doc[k])
                offsets[k] = self.out_offset(agency, doc[k])
        if fmt.get("utc_offset"):
            out["utc_offset"] = offsets
        return out

    def in_ts(self, agency: str, value: str) -> datetime:
        """A timestamp a client sent (e.g. updated_since). Zone-less input under the local knob is local wall time;
        the fall-back repeat is read as its FIRST occurrence (the earlier instant: a cursor may re-read, never
        skip), a spring-forward gap time forward. Without the knob: as before (zone-less = UTC)."""
        zi = self.local_zone(agency)
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is not None or zi is None:
            return parse_ts(value)
        a, b = dt.replace(tzinfo=zi, fold=0).astimezone(UTC), dt.replace(tzinfo=zi, fold=1).astimezone(UTC)
        return min(a, b) if a.astimezone(zi).replace(tzinfo=None) == dt else max(a, b)

    def wire_time(self, agency: str) -> str | int:
        """Telematics event time: RFC 3339 UTC, or epoch milliseconds under the knob (A110)."""
        if self.time_format(agency).get("epoch_ms"):
            return int((self.clock.now() - datetime(1970, 1, 1, tzinfo=UTC)).total_seconds() * 1000)
        return self.clock.iso()


TIME_FORMAT_OFF = {"local_tz": None, "utc_offset": False, "epoch_ms": False}


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

    @app.post("/sim/time-format")
    def time_format(body: dict = Body(default={}), x_agency: str = Header()):
        """Phase 10 Step 4.2 scenario knob (render-only; stored data stays UTC). `local_tz`: render zone-less local
        wall time in that zone (maintenance, workforce CSV); `utc_offset`: also send each field's offset;
        `epoch_ms`: telematics sends epoch milliseconds. An empty body switches everything off (the default)."""
        tz = body.get("local_tz") or None
        if tz is not None:
            try:
                ZoneInfo(tz)
            except (ZoneInfoNotFoundError, ValueError) as e:
                raise HTTPException(422, f"unknown zone {tz!r}") from e
        cfg = {"local_tz": tz, "utc_offset": bool(body.get("utc_offset")), "epoch_ms": bool(body.get("epoch_ms"))}
        ctx.store.put(x_agency, "config", "time_format", cfg)
        return cfg

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
