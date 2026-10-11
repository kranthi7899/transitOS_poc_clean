# ruff: noqa: B008
"""SIMULATED TripShot-style scheduling product. NO idempotency keys (duplicate calls duplicate
status-change rows); no write endpoint to cancel a block (A2)."""
from __future__ import annotations

from typing import Any

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query

from .chaos import Chaos
from .clock import SimClock, iso, parse_ts
from .common import Ctx, make_app
from .seed import build_fleet, type_ok


def create_app(clock: SimClock, chaos: Chaos, db_path: Any = None) -> FastAPI:
    ctx = Ctx("scheduling", clock, chaos, db_path)
    st = ctx.store
    tenant = Depends(ctx.tenant)
    KEYS = ("id", "seats", "wheelchair_positions", "lift_ok", "type", "status", "depot", "length_ft", "updated_at")
    IDENTITY = ("vin", "plate", "name")

    def seeder(a: str) -> None:
        now = iso(clock.now())
        fleet = build_fleet(a, clock.now())
        for v in fleet.vehicles:
            if v.get("sched") is None:   # Phase 10 Step 2: not in the revenue roster (retired, non-revenue, orphan)
                continue
            d = {k: v[k] for k in ("seats", "wheelchair_positions", "lift_ok", "type", "status", "depot")}
            d["length_ft"] = v.get("length_ft")  # optional (Phase 10): fleets that carry no length report null
            d.update({k: v[f"sched_{k}"] for k in IDENTITY if v.get(f"sched_{k}") is not None})  # Step 2 evidence
            st.put(a, "vehicles", v["sched"], {"id": v["sched"], **d, "updated_at": now})
        for b in fleet.blocks:
            blk = {k: b[k] for k in ("id", "vehicle", "operator", "start", "end", "start_depot", "needs")}
            # every block declares all its needs; a null length limit means "no limit declared" (Phase 10, F3)
            blk["needs"] = {"max_vehicle_length_ft": None, **blk["needs"]}
            st.put(a, "blocks", blk["id"], blk)

    app = make_app(ctx, "scheduling", seeder)

    def field_cfg(a: str) -> dict:
        return st.get(a, "config", "fields") or {}

    def shaped(a: str, d: dict, kind: str, rid: str) -> dict:
        """Render-time scenario switch (POST /sim/fields): drop keys or blank values ("" = a blank cell) of the
        vehicle records or of the blocks' `needs`, for every record or only the ids listed. Stored data is never
        changed, so the product's own write checks are unaffected."""
        cfg = field_cfg(a)
        part = cfg.get(kind) or {}
        if not part or (cfg.get("ids") is not None and rid not in cfg["ids"]):
            return d
        out = {k: v for k, v in d.items() if k not in set(part.get("drop", []))}
        out.update({k: "" for k in part.get("blank", []) if k in d})
        return out

    def render_vehicle(a: str, v: dict) -> dict:
        ident = {k: v[k] for k in IDENTITY if k in v}   # Step 2: VIN, plate, name only where the record holds them
        return shaped(a, {**{k: v.get(k) for k in KEYS}, **ident}, "vehicles", v["id"])

    def render_block(a: str, b: dict) -> dict:
        return {**b, "needs": shaped(a, dict(b["needs"]), "needs", b["id"])}

    @app.post("/sim/fields")
    def sim_fields(body: dict = Body(default={}), x_agency: str = Header()):
        """{"vehicles": {"drop": [...], "blank": [...]}, "needs": {"drop": [...], "blank": [...]}, "ids": [...]}
        (`id` and `status` are never dropped or blanked). An empty body restores the full payloads."""
        keep = {"id", "status"}
        cfg = {kind: {op: [k for k in (body.get(kind) or {}).get(op, []) if k not in keep]
                      for op in ("drop", "blank")} for kind in ("vehicles", "needs")}
        cfg["ids"] = body.get("ids")
        st.put(x_agency, "config", "fields", cfg)
        return cfg

    def vehicle(a: str, vid: str) -> dict:
        v = st.get(a, "vehicles", vid)
        if v is None:
            raise HTTPException(404, "vehicle not found")
        return v

    def set_status(a: str, v: dict, status: str, by: str, extra: dict | None = None) -> dict:
        now = iso(clock.now())
        v.update(status=status, updated_at=now, **(extra or {}))
        st.put(a, "vehicles", v["id"], v)
        n = st.next_seq(a, "change")
        st.put(a, "changes", f"{n:08d}", {"vehicle": v["id"], "status": status, "changed_at": now, "changed_by": by})
        return render_vehicle(a, v)

    def since_ok(ts: str, since: str | None) -> bool:
        return not since or parse_ts(ts) >= parse_ts(since)

    @app.get("/vehicles")
    def vehicles(status: str | None = None, updated_since: str | None = None, a: str = tenant):
        return [
            render_vehicle(a, v) for v in st.all(a, "vehicles")
            if (not status or v["status"] == status) and since_ok(v["updated_at"], updated_since)
        ]

    @app.get("/vehicles/{vid}")
    def one_vehicle(vid: str, a: str = tenant):
        return render_vehicle(a, vehicle(a, vid))

    @app.get("/blocks")
    def blocks(vehicle: str | None = None, from_: str | None = Query(default=None, alias="from"),
               to: str | None = None, a: str = tenant):
        out = []
        for b in st.all(a, "blocks"):
            if vehicle and b["vehicle"] != vehicle:
                continue
            if from_ and parse_ts(b["end"]) <= parse_ts(from_):
                continue
            if to and parse_ts(b["start"]) >= parse_ts(to):
                continue
            out.append(render_block(a, b))
        return out

    @app.get("/status-changes")
    def status_changes(since: str | None = None, a: str = tenant):
        return [c for c in st.all(a, "changes") if since_ok(c["changed_at"], since)]

    @app.post("/vehicles/{vid}/unavailable")
    def unavailable(vid: str, body: dict = Body(default={}), a: str = tenant):
        ctx.write_gate()
        v = vehicle(a, vid)
        return set_status(a, v, "out_of_service", body.get("changed_by") or "api",
                          {"unavailable": {"start": body.get("start"), "end": body.get("end")}})

    @app.post("/vehicles/{vid}/available")
    def available(vid: str, body: dict = Body(default={}), a: str = tenant):
        ctx.write_gate()
        return set_status(a, vehicle(a, vid), "ready", (body or {}).get("changed_by") or "api")

    @app.post("/blocks/{bid}/reassign")
    def reassign(bid: str, body: dict = Body(...), a: str = tenant,
                 x_restore: str | None = Header(default=None)):
        ctx.write_gate()
        b = st.get(a, "blocks", bid)
        if b is None:
            raise HTTPException(404, "block not found")
        target = vehicle(a, body.get("vehicle", ""))
        needs = b["needs"]
        if target["id"] == b["vehicle"]:
            raise HTTPException(409, "block already on that vehicle")
        # A57: a RESTORE of the block's immediately previous assignment (X-Restore: true) is accepted even
        # when that vehicle is out of service; a NEW assignment to an unavailable vehicle is still refused.
        # The earlier assignment already satisfied needs, so only double-booking is re-checked for a restore.
        restore = (x_restore or "").lower() == "true" and target["id"] == b.get("previous_vehicle")
        if not restore:
            if target["status"] == "out_of_service":
                raise HTTPException(409, "target vehicle out of service")
            if target["seats"] < needs.get("seats", 0):
                raise HTTPException(409, "target lacks seats")
            if target["wheelchair_positions"] < needs.get("wheelchair_positions", 0):
                raise HTTPException(409, "target lacks wheelchair positions")
            if needs.get("lift") and not target["lift_ok"]:
                raise HTTPException(409, "target lacks working lift")
            # PERMISSIVE ON PURPOSE (Phase 10, A14/A68): the product accepts any class at or above the block's
            # (type_ok is >=) and knows nothing of operator qualification or declared length limits; the
            # graded fit, length restriction and qualification live in transitos.clearance, not here.
            if not type_ok(target["type"], needs.get("type")):
                raise HTTPException(409, "target vehicle type unsuitable")
        s, e = parse_ts(b["start"]), parse_ts(b["end"])
        for o in st.all(a, "blocks"):
            if o["id"] != bid and o["vehicle"] == target["id"] and parse_ts(o["start"]) < e and parse_ts(o["end"]) > s:
                raise HTTPException(409, f"target vehicle already assigned to {o['id']}")
        b["previous_vehicle"] = None if restore else b["vehicle"]
        b["vehicle"] = target["id"]
        st.put(a, "blocks", bid, b)
        return b

    @app.post("/sim/vehicle-status")
    def sim_status(body: dict = Body(...), x_agency: str = Header()):
        """A human dispatcher changes a status in the product UI."""
        return set_status(x_agency, vehicle(x_agency, body["vehicle"]), body["status"], body.get("changed_by", "dispatcher"))

    return app
