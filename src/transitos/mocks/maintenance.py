# ruff: noqa: B008
"""SIMULATED maintenance product (generic shop-system shape; copies no product). Polled; Idempotency-Key honoured
(A1); 429 under chaos.

Phase 10 Step 4.1 (A105-A107): a `pending_repair` collection (POST/GET /pending-repairs) that a simulated foreman
(world engine ForemanActor, via /sim routes) converts into a work order after a delay. A work order born from a
pending repair speaks the shop status vocabulary OPEN, IN_PROGRESS, WAITING_PARTS, PENDING (deferred), COMPLETE,
CLOSED; others keep the legacy open/in_progress/closed unless /sim/config sets `shop_vocabulary`.
Phase 10 Step 4.2 (A108): under the agency's /sim/time-format knob every timestamp is rendered as zone-less local
wall time (optionally with a per-field `utc_offset` map). The store always keeps UTC; default OFF."""
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

    WO_TIMES = ("opened_at", "closed_at", "updated_at")
    PR_TIMES = ("created_at", "updated_at", "converted_at")
    SHOP_STATUSES = ("OPEN", "IN_PROGRESS", "WAITING_PARTS", "PENDING", "COMPLETE", "CLOSED")

    def out_wo(a: str, w: dict) -> dict:
        return ctx.out_doc(a, w, WO_TIMES)

    def out_pr(a: str, p: dict) -> dict:
        return ctx.out_doc(a, {**p, "simulated": True}, PR_TIMES)

    def is_closed(w: dict) -> bool:
        return str(w["status"]).lower() == "closed"

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
                if not is_closed(wo) and (now - parse_ts(wo["opened_at"])).total_seconds() >= c["auto_close_hours"] * 3600:
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
        wo.update(status="CLOSED" if str(wo["status"]).isupper() else "closed", closed_at=now, updated_at=now)
        if cancelled:
            wo["cancelled"] = True
        st.put(a, "wo", wo["id"], wo)
        return wo

    def since_ok(a: str, doc: dict, updated_since: str | None) -> bool:
        return not updated_since or parse_ts(doc["updated_at"]) >= ctx.in_ts(a, updated_since)

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
        return [ctx.out_doc(a, p, ("updated_at",)) for p in st.all(a, "pm") if since_ok(a, p, updated_since)]

    @app.get("/workorders")
    def list_wo(updated_since: str | None = None, asset: str | None = None, status: str | None = None, a: str = tenant):
        refresh(a)
        return [
            out_wo(a, w) for w in st.all(a, "wo")
            if since_ok(a, w, updated_since) and (not asset or w["asset"] == asset) and (not status or w["status"] == status)
        ]

    @app.get("/workorders/{wo_id}")
    def get_wo(wo_id: str, a: str = tenant):
        refresh(a)
        w = st.get(a, "wo", wo_id)
        if w is None:
            raise HTTPException(404, "work order not found")
        return out_wo(a, w)

    @app.post("/workorders")
    def create_wo(body: dict = Body(...), idempotency_key: str | None = Header(default=None), a: str = tenant):
        refresh(a)
        if idempotency_key:
            prior = st.get(a, "idem", idempotency_key)
            if prior:
                return out_wo(a, st.get(a, "wo", prior["wo_id"]))
        asset, desc = body.get("asset"), body.get("description", "")
        opened_by = body.get("opened_by", "human")
        if opened_by not in ("system", "human"):
            raise HTTPException(422, "opened_by must be system|human")
        if not asset or st.get(a, "assets", asset) is None:
            raise HTTPException(404, "asset not found")
        if st.get(a, "assets", asset).get("status") == "retired":   # Phase 10 Step 2 (A87)
            raise HTTPException(409, "asset is retired: no new work orders")
        wo = new_wo(a, asset, desc, opened_by, shop=bool(cfg(a).get("shop_vocabulary")))
        if idempotency_key:
            st.put(a, "idem", idempotency_key, {"wo_id": wo["id"]})
        return out_wo(a, wo)

    def new_wo(a: str, asset: str, desc: str, opened_by: str, shop: bool, **extra: Any) -> dict:
        n = st.next_seq(a, "wo")
        now = iso(clock.now())
        wo = {
            "id": f"wo-{n}", "wo_no": f"WO-{5000 + n}", "asset": asset, "status": "OPEN" if shop else "open",
            "description": desc, "opened_by": opened_by, "opened_at": now, "closed_at": None, "updated_at": now,
            **extra,
        }
        st.put(a, "wo", wo["id"], wo)
        return wo

    @app.post("/workorders/{wo_id}/cancel")
    def cancel_wo(wo_id: str, a: str = tenant):
        refresh(a)
        w = st.get(a, "wo", wo_id)
        if w is None:
            raise HTTPException(404, "work order not found")
        if w["opened_by"] != "system":
            raise HTTPException(409, "only system-opened work orders can be cancelled")
        if is_closed(w):
            return out_wo(a, w)  # idempotent
        return out_wo(a, close_wo(a, w, cancelled=True))

    # ---- Phase 10 Step 4.1: pending repairs (A105) ----
    def get_pr(a: str, pr_id: str) -> dict:
        p = st.get(a, "pending_repair", pr_id)
        if p is None:
            raise HTTPException(404, "pending repair not found")
        return p

    @app.post("/pending-repairs")
    def create_pr(body: dict = Body(...), idempotency_key: str | None = Header(default=None), a: str = tenant):
        """A repair request for an asset, not yet on a work order. Idempotency-Key honoured: a repeat returns the
        request in its CURRENT state (converted or not), never a second request."""
        refresh(a)
        if idempotency_key and (prior := st.get(a, "pr_idem", idempotency_key)):
            return out_pr(a, get_pr(a, prior["pr_id"]))
        asset = body.get("asset")
        by = body.get("requested_by", "human")
        if by not in ("system", "human"):
            raise HTTPException(422, "requested_by must be system|human")
        if not asset or st.get(a, "assets", asset) is None:
            raise HTTPException(404, "asset not found")
        if st.get(a, "assets", asset).get("status") == "retired":
            raise HTTPException(409, "asset is retired: no new repairs")
        n = st.next_seq(a, "pr")
        now = iso(clock.now())
        p = {"id": f"pr-{n}", "pr_no": f"PR-{7000 + n}", "asset": asset, "description": body.get("description", ""),
             "requested_by": by, "status": "OPEN", "work_order": None, "idempotency_key": idempotency_key,
             "created_at": now, "updated_at": now, "converted_at": None}
        st.put(a, "pending_repair", p["id"], p)
        if idempotency_key:
            st.put(a, "pr_idem", idempotency_key, {"pr_id": p["id"]})
        return out_pr(a, p)

    @app.get("/pending-repairs")
    def list_pr(asset: str | None = None, status: str | None = None, idempotency_key: str | None = None,
                a: str = tenant):
        refresh(a)
        return [out_pr(a, p) for p in st.all(a, "pending_repair")
                if (not asset or p["asset"] == asset) and (not status or p["status"] == status)
                and (not idempotency_key or p.get("idempotency_key") == idempotency_key)]

    @app.get("/pending-repairs/{pr_id}")
    def read_pr(pr_id: str, a: str = tenant):
        refresh(a)
        return out_pr(a, get_pr(a, pr_id))

    @app.post("/pending-repairs/{pr_id}/cancel")
    def cancel_pr(pr_id: str, a: str = tenant):
        """Withdraw a request. An OPEN request, or one attached to a work order it did not create, is CANCELLED
        (that work order is untouched). A request that created its work order: cancel the work order (409)."""
        refresh(a)
        p = get_pr(a, pr_id)
        if p["status"] == "CANCELLED":
            return out_pr(a, p)
        if p["status"] == "CONVERTED":
            w = st.get(a, "wo", p["work_order"])
            if w is not None and w.get("from_pending_repair") == pr_id:
                raise HTTPException(409, f"pending repair created work order {w['id']}: cancel that work order")
        p.update(status="CANCELLED", updated_at=iso(clock.now()))
        st.put(a, "pending_repair", pr_id, p)
        return out_pr(a, p)

    # ---- sim controls (no chaos guard) ----
    @app.post("/sim/workorders/{wo_id}/close")
    def sim_close(wo_id: str, x_agency: str = Header()):
        w = st.get(x_agency, "wo", wo_id)
        if w is None:
            raise HTTPException(404, "work order not found")
        return out_wo(x_agency, close_wo(x_agency, w))

    @app.post("/sim/pending-repairs/{pr_id}/convert")
    def sim_convert(pr_id: str, x_agency: str = Header()):
        """The foreman puts a pending repair on a work order (A106). A work order already open for the asset (for
        example one a person opened first) takes it; otherwise a new shop-vocabulary work order is created from
        it, opened on behalf of whoever requested the repair. A cancelled request is never converted."""
        a = x_agency
        p = get_pr(a, pr_id)
        if p["status"] == "CONVERTED":
            return {"pending_repair": out_pr(a, p), "work_order": out_wo(a, st.get(a, "wo", p["work_order"]))}
        if p["status"] == "CANCELLED":
            return {"pending_repair": out_pr(a, p), "work_order": None}
        live = [w for w in st.all(a, "wo") if w["asset"] == p["asset"] and not is_closed(w)]
        now = iso(clock.now())
        if live:
            w = live[0]
            w.update(updated_at=now, pending_repairs=[*w.get("pending_repairs", []), pr_id])
            st.put(a, "wo", w["id"], w)
        else:
            w = new_wo(a, p["asset"], p["description"], p["requested_by"], shop=True, from_pending_repair=pr_id,
                       pending_repairs=[pr_id])
        p.update(status="CONVERTED", work_order=w["id"], converted_at=now, updated_at=now)
        st.put(a, "pending_repair", pr_id, p)
        return {"pending_repair": out_pr(a, p), "work_order": out_wo(a, w)}

    @app.post("/sim/workorders/{wo_id}/status")
    def sim_status(wo_id: str, body: dict = Body(...), x_agency: str = Header()):
        """The foreman moves a shop work order through IN_PROGRESS, WAITING_PARTS, PENDING (deferred), COMPLETE.
        Closing goes through /close (sets closed_at)."""
        w = st.get(x_agency, "wo", wo_id)
        if w is None:
            raise HTTPException(404, "work order not found")
        status = body.get("status")
        if status not in SHOP_STATUSES[:-1]:
            raise HTTPException(422, f"status must be one of {SHOP_STATUSES[:-1]}")
        if is_closed(w):
            raise HTTPException(409, "work order is closed")
        w.update(status=status, updated_at=iso(clock.now()))
        st.put(x_agency, "wo", wo_id, w)
        return out_wo(x_agency, w)

    @app.post("/sim/workorders/{wo_id}/progress")
    def sim_progress(wo_id: str, x_agency: str = Header()):
        w = st.get(x_agency, "wo", wo_id)
        if w is None:
            raise HTTPException(404, "work order not found")
        w.update(status="IN_PROGRESS" if str(w["status"]).isupper() else "in_progress", updated_at=iso(clock.now()))
        st.put(x_agency, "wo", wo_id, w)
        return w

    @app.post("/sim/meter")
    def sim_meter(body: dict = Body(...), x_agency: str = Header()):
        set_meter(x_agency, body["asset"], int(body["meter_mi"]))
        return {"ok": True}

    @app.post("/sim/config")
    def sim_config(body: dict = Body(...), x_agency: str = Header()):
        c = cfg(x_agency)
        for k in ("auto_close_hours", "miles_per_hour", "shop_vocabulary"):
            if k in body:
                c[k] = body[k]
        st.put(x_agency, "config", "cfg", c)
        return c

    return app
