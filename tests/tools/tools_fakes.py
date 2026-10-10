"""Minimal fakes of the four mock products, written straight from docs/MOCK_API.md (Track A's real
mocks were not on main). `World` holds all state; `World.clients()` returns TestClients per product."""
from __future__ import annotations

import itertools
from datetime import UTC, datetime

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.testclient import TestClient

from transitos.tools.common import StaticCrosswalk

AGENCY = "agency-042"
T0 = datetime(2026, 3, 2, 8, 0, tzinfo=UTC)
T1 = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)


class World:
    def __init__(self) -> None:
        self.down: set[str] = set()          # products returning 503
        self.flaky: dict[str, int] = {}      # product -> number of upcoming 429s
        self.calls: list[tuple[str, str, str]] = []  # (product, method, path)
        self._ids = itertools.count(1)
        self.vehicles = {
            "BUS-44": {"id": "BUS-44", "seats": 30, "wheelchair_positions": 2, "lift_ok": True, "type": "bus", "status": "in_service", "depot": "north"},
            "BUS-45": {"id": "BUS-45", "seats": 30, "wheelchair_positions": 2, "lift_ok": True, "type": "bus", "status": "ready", "depot": "north"},
            "BUS-46": {"id": "BUS-46", "seats": 20, "wheelchair_positions": 0, "lift_ok": False, "type": "bus", "status": "ready", "depot": "north"},
        }
        self.blocks = {
            "B-1": {"id": "B-1", "vehicle": "BUS-44", "operator": "OP-310", "start": T0.isoformat(), "end": T1.isoformat(),
                        "start_depot": "north", "needs": {"seats": 25, "wheelchair_positions": 1, "lift": True, "type": None}},
        }
        self.workorders: dict[str, dict] = {}
        self.wo_by_key: dict[str, str] = {}
        self.status_changes: list[dict] = []
        self.alerts: list[dict] = []
        self.operators = {
            "OP-310": {"operator": "OP-310", "qualifications": "bus", "lift_trained": "true", "hours_today": "2.0"},
            "OP-311": {"operator": "OP-311", "qualifications": "bus", "lift_trained": "true", "hours_today": "2.0"},
            "OP-312": {"operator": "OP-312", "qualifications": "bus", "lift_trained": "false", "hours_today": "1.0"},
            "OP-313": {"operator": "OP-313", "qualifications": "bus", "lift_trained": "true", "hours_today": "7.5"},
        }

    # -- helpers
    def gate(self, product: str, method: str, path: str) -> None:
        self.calls.append((product, method, path))
        if product in self.down:
            raise HTTPException(503, "down")
        if self.flaky.get(product, 0) > 0:
            self.flaky[product] -= 1
            raise HTTPException(429, "slow down")

    def count(self, product: str, method: str, path_prefix: str) -> int:
        return sum(1 for p, m, pa in self.calls if p == product and m == method and pa.startswith(path_prefix))

    def seed_wo(self, asset: str, opened_by: str = "human", status: str = "open") -> dict:
        n = next(self._ids)
        wo = {"id": str(900 + n), "wo_no": f"WO-{900 + n}", "asset": asset, "status": status, "description": "seeded",
                  "opened_by": opened_by, "opened_at": T0.isoformat(), "closed_at": None, "updated_at": T0.isoformat()}
        self.workorders[wo["id"]] = wo
        return wo

    def crosswalk(self, agency: str = AGENCY) -> StaticCrosswalk:
        cw = StaticCrosswalk()
        cw.add(agency, "veh-1", scheduling="BUS-44", maintenance="1106", telematics="T-77")
        cw.add(agency, "veh-2", scheduling="BUS-45", maintenance="1107", telematics="T-78")
        cw.add(agency, "veh-3", scheduling="BUS-46", maintenance="1108", telematics="T-79")
        for n in (310, 311, 312, 313):
            cw.add(agency, f"op-{n}", workforce=f"OP-{n}")
        return cw

    def clients(self) -> dict[str, TestClient]:
        return {"maintenance": TestClient(self._maint()), "scheduling": TestClient(self._sched()),
                "workforce": TestClient(self._work()), "alerts": TestClient(self._alerts())}

    # -- apps
    def _maint(self) -> FastAPI:
        app, w = FastAPI(), self

        @app.get("/workorders")
        def list_wo(asset: str | None = None, status: str | None = None):
            w.gate("maintenance", "GET", "/workorders")
            return [x for x in w.workorders.values() if (asset is None or x["asset"] == asset)
                    and (status is None or x["status"] == status)]

        @app.get("/workorders/{wid}")
        def get_wo(wid: str):
            w.gate("maintenance", "GET", f"/workorders/{wid}")
            if wid not in w.workorders:
                raise HTTPException(404)
            return w.workorders[wid]

        @app.post("/workorders")
        async def create(request: Request, idempotency_key: str | None = Header(default=None)):
            w.gate("maintenance", "POST", "/workorders")
            body = await request.json()
            if idempotency_key and idempotency_key in w.wo_by_key:
                return w.workorders[w.wo_by_key[idempotency_key]]
            n = next(w._ids)
            wo = {"id": str(500 + n), "wo_no": f"WO-{500 + n}", "asset": body["asset"], "status": "open",
                      "description": body["description"], "opened_by": body["opened_by"], "opened_at": T0.isoformat(),
                      "closed_at": None, "updated_at": T0.isoformat()}
            w.workorders[wo["id"]] = wo
            if idempotency_key:
                w.wo_by_key[idempotency_key] = wo["id"]
            return wo

        @app.post("/workorders/{wid}/cancel")
        def cancel(wid: str):
            w.gate("maintenance", "POST", f"/workorders/{wid}/cancel")
            wo = w.workorders.get(wid)
            if not wo:
                raise HTTPException(404)
            if wo["opened_by"] != "system":
                raise HTTPException(409, "only system-created")
            wo["status"] = "closed"
            return wo
        return app

    def _sched(self) -> FastAPI:
        app, w = FastAPI(), self

        @app.get("/vehicles")
        def vehicles(status: str | None = None):
            w.gate("scheduling", "GET", "/vehicles")
            return [v for v in w.vehicles.values() if status is None or v["status"] == status]

        @app.get("/vehicles/{vid}")
        def vehicle(vid: str):
            w.gate("scheduling", "GET", f"/vehicles/{vid}")
            if vid not in w.vehicles:
                raise HTTPException(404)
            return w.vehicles[vid]

        @app.get("/blocks")
        def blocks(vehicle: str | None = None, frm: str | None = Query(default=None, alias="from"), to: str | None = None):
            w.gate("scheduling", "GET", "/blocks")
            return [b for b in w.blocks.values() if vehicle is None or b["vehicle"] == vehicle]

        @app.get("/status-changes")
        def changes(since: str | None = None):
            w.gate("scheduling", "GET", "/status-changes")
            return w.status_changes

        @app.post("/vehicles/{vid}/unavailable")
        async def unavail(vid: str, request: Request):
            w.gate("scheduling", "POST", f"/vehicles/{vid}/unavailable")
            w.vehicles[vid]["status"] = "out_of_service"
            w.status_changes.append({"vehicle": vid, "status": "out_of_service", "changed_at": T0.isoformat(), "changed_by": "api"})
            return {"ok": True}

        @app.post("/vehicles/{vid}/available")
        def avail(vid: str):
            w.gate("scheduling", "POST", f"/vehicles/{vid}/available")
            w.vehicles[vid]["status"] = "ready"
            return {"ok": True}

        @app.post("/blocks/{bid}/reassign")
        async def reassign(bid: str, request: Request):
            w.gate("scheduling", "POST", f"/blocks/{bid}/reassign")
            w.blocks[bid]["vehicle"] = (await request.json())["vehicle"]
            return w.blocks[bid]
        return app

    def _work(self) -> FastAPI:
        app, w = FastAPI(), self

        @app.get("/export/operators.csv")
        def csv_export():
            w.gate("workforce", "GET", "/export/operators.csv")
            cols = "operator,qualifications,lift_trained,hours_today,hours_week,available_from,available_to,on_extra_board"
            rows = [cols] + [f"{o['operator']},{o['qualifications']},{o['lift_trained']},{o['hours_today']},20,,,false"
                             for o in w.operators.values()]
            return Response("\n".join(rows) + "\n", media_type="text/csv")

        @app.post("/blocks/{bid}/operator")
        async def assign(bid: str, request: Request):
            w.gate("workforce", "POST", f"/blocks/{bid}/operator")
            w.blocks[bid]["operator"] = (await request.json())["operator"]
            return w.blocks[bid]
        return app

    def _alerts(self) -> FastAPI:
        app, w = FastAPI(), self

        @app.post("/alerts")
        async def post_alert(request: Request):
            w.gate("alerts", "POST", "/alerts")
            body = await request.json()
            a = dict(id=f"AL-{len(w.alerts) + 1}", **body)
            w.alerts.append(a)
            return {"id": a["id"]}
        return app
