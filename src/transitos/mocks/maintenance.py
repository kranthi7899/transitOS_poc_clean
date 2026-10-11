# ruff: noqa: B008
"""SIMULATED FASTER-style maintenance product. Polled; Idempotency-Key honoured (A1); 429 under chaos."""
from __future__ import annotations

from typing import Any

from fastapi import Body, Depends, FastAPI, Header, HTTPException

from .chaos import Chaos
from .clock import SimClock, iso, parse_ts
from .common import Ctx, make_app
from .seed import build_fleet


def create_app(clock: SimClock, chaos: Chaos, db_path: Any = None) -> FastAPI:
    ctx = Ctx("maintenance", clock, chaos, db_path)
    st = ctx.store

    def seeder(a: str) -> None:
        now = iso(clock.now())
        fleet = build_fleet(a, clock.now())
        for v in fleet.vehicles:
            if v.get("maint") is None:   # Phase 10 Step 2: no maintenance asset (an orphan telematics unit)
                continue
            pm = next((p for p in fleet.pm if p["vehicle_idx"] == v["idx"]), None)
            st.put(a, "assets", v["maint"], {
                "asset": v["maint"], "description": f"{v['type']} coach {v['name']}",
                "status": v.get("maint_status", "active"), "meter_mi": pm["meter_mi"] if pm else 0,
                **{k: v[f"maint_{k}"] for k in ("vin", "plate", "name", "make") if v.get(f"maint_{k}")},  # Step 2
            })
            if pm is None:   # no PM schedule for a retired asset
                continue
            st.put(a, "pm", v["maint"], {
                "asset": v["maint"], "service_code": pm["service_code"], "due_mi": pm["due_mi"],
                "meter_mi": pm["meter_mi"], "updated_at": now,
            })
        st.put(a, "config", "cfg", {"auto_close_hours": None, "miles_per_hour": 0.0, "last_tick": now})

    app = make_app(ctx, "maintenance", seeder)
    tenant = Depends(ctx.tenant)

    def cfg(a: str) -> dict:
        return st.get(a, "config", "cfg") or {"auto_close_hours": None, "miles_per_hour": 0.0, "last_tick": iso(clock.now())}

    def refresh(a: str) -> None:
        """Sim-clock driven state: mileage accrual (drives PMOverdue) and WO auto-close."""
        c, now = cfg(a), clock.now()
        hours = (now - parse_ts(c["last_tick"])).total_seconds() / 3600
        if hours > 0 and c["miles_per_hour"]:
            for asset in st.all(a, "assets"):
                if asset["status"] == "active":
                    set_meter(a, asset["asset"], asset["meter_mi"] + int(hours * c["miles_per_hour"]))
        if c["auto_close_hours"] is not None:
            for wo in st.all(a, "wo"):
                if wo["status"] != "closed" and (now - parse_ts(wo["opened_at"])).total_seconds() >= c["auto_close_hours"] * 3600:
                    close_wo(a, wo)
        c["last_tick"] = iso(now)
        st.put(a, "config", "cfg", c)

    def set_meter(a: str, asset_id: str, meter: int) -> None:
        asset = st.get(a, "assets", asset_id)
        if asset is None:
            raise HTTPException(404, "asset not found")
        now = iso(clock.now())
        asset["meter_mi"] = meter
        st.put(a, "assets", asset_id, asset)
        pm = st.get(a, "pm", asset_id)
        if pm:
            pm.update(meter_mi=meter, updated_at=now)
            st.put(a, "pm", asset_id, pm)

    def close_wo(a: str, wo: dict, cancelled: bool = False) -> dict:
        now = iso(clock.now())
        wo.update(status="closed", closed_at=now, updated_at=now)
        if cancelled:
            wo["cancelled"] = True
        st.put(a, "wo", wo["id"], wo)
        return wo

    def since_ok(doc: dict, updated_since: str | None) -> bool:
        return not updated_since or parse_ts(doc["updated_at"]) >= parse_ts(updated_since)

    @app.get("/assets")
    def list_assets(a: str = tenant):   # Phase 10 Step 2: the asset register (unit, VIN, plate, name, make, status)
        refresh(a)
        return sorted(st.all(a, "assets"), key=lambda d: d["asset"])

    @app.get("/assets/{asset}")
    def get_asset(asset: str, a: str = tenant):
        refresh(a)
        d = st.get(a, "assets", asset)
        if d is None:
            raise HTTPException(404, "asset not found")
        return d

    @app.get("/pm-schedules")
    def pm_schedules(updated_since: str | None = None, a: str = tenant):
        refresh(a)
        return [p for p in st.all(a, "pm") if since_ok(p, updated_since)]

    @app.get("/workorders")
    def list_wo(updated_since: str | None = None, asset: str | None = None, status: str | None = None, a: str = tenant):
        refresh(a)
        return [
            w for w in st.all(a, "wo")
            if since_ok(w, updated_since) and (not asset or w["asset"] == asset) and (not status or w["status"] == status)
        ]

    @app.get("/workorders/{wo_id}")
    def get_wo(wo_id: str, a: str = tenant):
        refresh(a)
        w = st.get(a, "wo", wo_id)
        if w is None:
            raise HTTPException(404, "work order not found")
        return w

    @app.post("/workorders")
    def create_wo(body: dict = Body(...), idempotency_key: str | None = Header(default=None), a: str = tenant):
        refresh(a)
        if idempotency_key:
            prior = st.get(a, "idem", idempotency_key)
            if prior:
                return st.get(a, "wo", prior["wo_id"])
        asset, desc = body.get("asset"), body.get("description", "")
        opened_by = body.get("opened_by", "human")
        if opened_by not in ("system", "human"):
            raise HTTPException(422, "opened_by must be system|human")
        if not asset or st.get(a, "assets", asset) is None:
            raise HTTPException(404, "asset not found")
        if st.get(a, "assets", asset).get("status") == "retired":   # Phase 10 Step 2 (A87)
            raise HTTPException(409, "asset is retired: no new work orders")
        n = st.next_seq(a, "wo")
        now = iso(clock.now())
        wo = {
            "id": f"wo-{n}", "wo_no": f"WO-{5000 + n}", "asset": asset, "status": "open",
            "description": desc, "opened_by": opened_by, "opened_at": now, "closed_at": None, "updated_at": now,
        }
        st.put(a, "wo", wo["id"], wo)
        if idempotency_key:
            st.put(a, "idem", idempotency_key, {"wo_id": wo["id"]})
        return wo

    @app.post("/workorders/{wo_id}/cancel")
    def cancel_wo(wo_id: str, a: str = tenant):
        refresh(a)
        w = st.get(a, "wo", wo_id)
        if w is None:
            raise HTTPException(404, "work order not found")
        if w["opened_by"] != "system":
            raise HTTPException(409, "only system-opened work orders can be cancelled")
        if w["status"] == "closed":
            return w  # idempotent
        return close_wo(a, w, cancelled=True)

    # ---- sim controls (no chaos guard) ----
    @app.post("/sim/workorders/{wo_id}/close")
    def sim_close(wo_id: str, x_agency: str = Header()):
        w = st.get(x_agency, "wo", wo_id)
        if w is None:
            raise HTTPException(404, "work order not found")
        return close_wo(x_agency, w)

    @app.post("/sim/workorders/{wo_id}/progress")
    def sim_progress(wo_id: str, x_agency: str = Header()):
        w = st.get(x_agency, "wo", wo_id)
        if w is None:
            raise HTTPException(404, "work order not found")
        w.update(status="in_progress", updated_at=iso(clock.now()))
        st.put(x_agency, "wo", wo_id, w)
        return w

    @app.post("/sim/meter")
    def sim_meter(body: dict = Body(...), x_agency: str = Header()):
        set_meter(x_agency, body["asset"], int(body["meter_mi"]))
        return {"ok": True}

    @app.post("/sim/config")
    def sim_config(body: dict = Body(...), x_agency: str = Header()):
        c = cfg(x_agency)
        for k in ("auto_close_hours", "miles_per_hour"):
            if k in body:
                c[k] = body[k]
        st.put(x_agency, "config", "cfg", c)
        return c

    return app
