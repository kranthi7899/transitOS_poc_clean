"""Local dispatcher demo over the real integrated System, never a second workflow (A198)."""
from __future__ import annotations

import copy
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from transitos.clearance.evaluate import Assign, Option, safe_evaluate
from transitos.clearance.facts import Facts
from transitos.contracts.policy import AgencyPolicy
from transitos.verification.invariants import check_all
from transitos.verification.scenarios import AGENCY, BY_ID
from transitos.workflow.memory import IncidentMemory, MemoryReasoner
from transitos.workflow.reasoning import Reasoner, reasoner_from_env

from .product_views import PRODUCTS, read_identity, read_products, update_notifications
from .world import IntegrationWorld

ASSETS = Path(__file__).resolve().parents[1] / "panel" / "console"


class Command(BaseModel):
    action: Literal["trigger", "approve", "reject", "repair", "advance", "reset", "pause", "resume", "refresh"]
    generation: str
    plan_version: int | None = None
    scope: Literal["plan", "return_to_service"] = "plan"
    user: str = Field(default="Dispatcher", min_length=1, max_length=80)
    acknowledged: list[str] = Field(default_factory=list, max_length=50)


class GraphObserver:
    """Capture actual node updates while leaving graph/interrupt semantics untouched."""

    def __init__(self, graph, session):
        self.graph, self.session = graph, session

    def __getattr__(self, name):
        return getattr(self.graph, name)

    def invoke(self, value, config, **kw):
        for update in self.graph.stream(value, config, stream_mode="updates", **kw):
            for node, data in update.items():
                if node == "__interrupt__":
                    self.session.trace.append({"node": "interrupt", "status": "Waiting for a person or product event",
                                               "time": self.session.system.clock.iso()})
                else:
                    self.session.trace.append({"node": node, "status": "Completed",
                                               "time": self.session.system.clock.iso()})
                    if node in ("reason", "revalidate") and isinstance(data, dict):
                        self.session.capture_assessment(data)
                self.session.publish(data.get("run") if isinstance(data, dict) else None)
        snapshot = self.graph.get_state(config)
        for task in snapshot.tasks:
            if task.interrupts:
                self.session.trace.append({"node": task.name, "status": "Interrupt · waiting",
                                           "time": self.session.system.clock.iso()})
        self.session.publish()
        return snapshot.values


class DemoSession:
    def __init__(self, reasoner: Reasoner | None = None, memory: IncidentMemory | None = None, *,
                 world_factory=IntegrationWorld, agency: str = AGENCY):
        self.agency = agency
        self.memory = memory or IncidentMemory()
        self.reasoner = MemoryReasoner(reasoner or reasoner_from_env(), self.memory, agency)
        self.lock = threading.RLock()
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="transitos-demo")
        self.busy = False
        self.error: str | None = None
        self.trace: list[dict] = []
        self.world = world_factory(reasoner=self.reasoner, bus="sqlite", db="sqlite")
        self.snapshot: dict = {}
        self.reset()

    @property
    def system(self):
        return self.world.system

    def reset(self):
        self.generation = uuid.uuid4().hex
        self.trace = []
        self.assessment = None
        self.notifications, self.notification_keys = [], set()
        self.product_baseline = None
        self.world.apply(BY_ID["operator_swap_needed"])
        self.system.graph = GraphObserver(self.system.graph, self)
        self.publish()

    def capture_assessment(self, data):
        """Expand the existing pure clearance result for display; never invent passed checks.

        Plan.clearance deliberately stores only noteworthy items. The console also shows native
        passes by evaluating the exact plan on the workflow's own proposal/revalidation facts.
        """
        run, mf = data.get("run") or {}, (data.get("ctx") or {}).get("mf") or {}
        plan = run.get("plan")
        if not plan or not mf.get("clearance"):
            return
        steps = plan["steps"]
        operators = {s["params"]["block_id"]: s["params"]["operator_id"]
                     for s in steps if s["action"] == "assign_operator"}
        option = Option(tuple(Assign(s["params"]["block_id"], s["params"]["to_vehicle_id"],
                                    operators.get(s["params"]["block_id"]))
                              for s in steps if s["action"] == "reassign_block"))
        result = safe_evaluate(option, Facts.from_dict(mf["clearance"]),
                               AgencyPolicy.model_validate(mf["clearance_policy"]), datetime.fromisoformat(mf["now"]))
        self.assessment = {"at": mf["now"], "plan_version": run["plan_version"],
                           "items": [i.model_dump() for i in result.items]}

    def publish(self, run_hint: dict | None = None):
        obs = self.world.observe()
        data = obs.model_dump(mode="json")
        if run_hint:
            data["runs"] = [run_hint]
        run = data["runs"][0] if data["runs"] else None
        violations = check_all(obs)
        if run and run["status"] == "DONE" and not violations:
            self.memory.remember(obs.runs[0], obs, self.generation)
        history = self.system.decisions.list_for_run(run["run_id"]) if run else []
        budget = getattr(self.reasoner, "budget", None)
        spend = budget.status() if budget and hasattr(budget, "status") else None
        data.update({"generation": self.generation, "clock": self.system.clock.iso(),
                     "reasoner": self.reasoner.name, "spend": spend,
                     "assessment": self.assessment,
                     "memory": self.memory.recall(self.agency, run["vehicle_id"] if run else "veh-1"),
                     "trace": list(self.trace[-120:]),
                     "history": [r.model_dump(mode="json") for r in history],
                     "identities": {id: self.system.xw.all_ids(self.agency, id,
                                        entities=("operator",) if id.startswith("op-") else ("vehicle", "asset"))
                                    for id in [*[v.id for v in obs.vehicles], *[o.id for o in obs.operators]]},
                     "visible_alerts": self.world.mocks.clients["alerts"].get("/alerts", headers=self.world._h).json(),
                     "invariant_violations": [v.model_dump() for v in violations],
                     "storage_profile": getattr(self.world, "storage_profile", "local"),
                     "connected_instance": getattr(self.world, "instance", None),
                     "observed_at": datetime.now(UTC).isoformat()})
        products = read_products(self.world)
        if self.product_baseline is None:
            self.product_baseline = copy.deepcopy(products)
        update_notifications(self, data)
        data.update({"product_records": products, "product_baseline": self.product_baseline,
                     "notifications": list(self.notifications), "identity_bridge": read_identity(self.world)})
        with self.lock:
            self.snapshot = data

    def state(self) -> dict:
        with self.lock:
            return {**copy.deepcopy(self.snapshot), "busy": self.busy, "error": self.error}

    def validate(self, command: Command):
        if command.generation != self.generation:
            raise HTTPException(409, "This demo was reset. Refresh before acting.")
        state = self.snapshot
        run = state["runs"][0] if state["runs"] else None
        if command.action == "trigger" and run:
            raise HTTPException(409, "An incident already exists. Reset to rehearse again.")
        if command.action in ("approve", "reject"):
            if not run or command.plan_version != run["plan_version"]:
                raise HTTPException(409, "The proposal changed. Refresh and review it again.")
            expected = "RETURNING" if command.scope == "return_to_service" else "AWAITING_APPROVAL"
            if run["status"] != expected:
                raise HTTPException(409, "This incident is not waiting for that approval.")
            if not command.user.strip():
                raise HTTPException(422, "Enter the dispatcher name.")
            if command.action == "approve" and command.scope == "plan":
                missing = {i["rule"] for i in run["plan"]["clearance"] if i["requires_ack"]} - set(command.acknowledged)
                if missing:
                    raise HTTPException(409, "Acknowledge the flagged checks before approving.")
        if command.action == "repair" and (not run or run["status"] != "WAITING_FOR_REPAIR"):
            raise HTTPException(409, "Repair completion is available only while waiting for repair.")
        if command.action == "approve" and state["policy"]["kill_switch"]:
            raise HTTPException(409, "Resume the agent before approving execution.")

    def submit(self, command: Command):
        with self.lock:
            if self.busy:
                raise HTTPException(409, "An action is in progress. Wait for it to finish.")
            self.validate(command)
            self.busy, self.error = True, None
            self.worker.submit(self.perform, command)

    def perform(self, command: Command):
        try:
            s = self.system
            run = self.snapshot["runs"][0] if self.snapshot["runs"] else None
            if command.action == "reset":
                self.reset()
            elif command.action == "refresh":
                # Poll native product events; only already-authorised workflow work may execute.
                s.run_until_idle()
            elif command.action == "trigger":
                self.world.mocks.clients["telematics"].post("/sim/fault", headers=self.world._h,
                    json={"vehicle": "T-77", "spn": 520, "fmi": 3, "lamps": {"stop": True}}).raise_for_status()
                s.run_until_idle()
            elif command.action in ("approve", "reject"):
                response = s.panel_client.post(f"/runs/{run['run_id']}/decision", json={
                    "decision": command.action, "plan_version": command.plan_version, "scope": command.scope,
                    "user": command.user.strip(), "acknowledged": command.acknowledged})
                if response.status_code >= 400:
                    raise ValueError("The workflow refused the decision. Refresh and review the current plan.")
                s.run_until_idle()
            elif command.action == "repair":
                s.clock.advance(hours=48)
                self.world.close_work_order(run["run_id"])
            elif command.action == "advance":
                self.world.advance_clock(5 / 60)
            elif command.action in ("pause", "resume"):
                self.world.set_kill_switch(command.action == "pause")
            self.publish()
        except Exception as exc:  # noqa: BLE001 - do not send provider errors/secrets to the browser
            with self.lock:
                self.error = f"Action could not finish ({type(exc).__name__}). Review the incident before trying again."
            self.publish()
        finally:
            with self.lock:
                self.busy = False

    def close(self):
        self.worker.shutdown(wait=True)
        self.system.close()
        self.world._drop_data_dir()


def create_app(reasoner: Reasoner | None = None, memory: IncidentMemory | None = None, *,
               session: DemoSession | None = None) -> FastAPI:
    session = session or DemoSession(reasoner, memory)

    @asynccontextmanager
    async def lifespan(app):
        yield
        session.close()

    app = FastAPI(title="TransitOS simulated dispatcher console", lifespan=lifespan)
    app.state.demo = session

    @app.middleware("http")
    async def demo_boundary(request: Request, call_next):
        # Local demo only; custom JSON header forces a preflight for foreign browser origins.
        host = request.url.hostname
        if host not in ("127.0.0.1", "localhost", "testserver"):
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "This demo is localhost only"}, status_code=403)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if request.headers.get("x-transitos-demo") != "1" or (
                origin and urlparse(origin).netloc != request.url.netloc
            ):
                from fastapi.responses import JSONResponse
                return JSONResponse({"detail": "Same-origin demo action required"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
        return response

    @app.get("/api/demo/state")
    def state():
        return session.state()

    @app.post("/api/demo/command", status_code=202)
    def command(body: Command):
        session.submit(body)
        return {"accepted": True}

    @app.get("/")
    def index():
        return FileResponse(ASSETS / "index.html")

    @app.get("/products/{product}")
    def product_workspace(product: str):
        if product not in PRODUCTS:
            raise HTTPException(404, "Unknown simulated product")
        return FileResponse(ASSETS / "product.html")

    app.mount("/assets", StaticFiles(directory=ASSETS), name="assets")
    return app


def main():
    import uvicorn
    print("TransitOS dispatcher demo — SIMULATED products")
    print("Open http://127.0.0.1:8110 — Ctrl+C to stop")
    uvicorn.run(create_app(), host="127.0.0.1", port=8110, log_level="warning")


if __name__ == "__main__":
    main()
