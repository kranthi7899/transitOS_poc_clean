"""Existing workflow/console over real HTTP product boundaries, no replacement graph."""
from __future__ import annotations

import httpx
from fastapi import HTTPException

from transitos.mocks.clock import SimClock, parse_ts
from transitos.workflow.memory import IncidentMemory

from .connected import AGENCY, BASE, PORTS
from .console import Command, DemoSession, create_app
from .system import Mocks, System
from .world import IntegrationWorld


class ConnectedWorld(IntegrationWorld):
    storage_profile = "databricks"

    def __init__(self, *, instance: str, clients=None, data_dir=None, **kwargs):
        super().__init__(**kwargs)
        self.instance, self.clients, self.data_dir = instance, clients, data_dir

    def apply(self, scenario):
        if self.system is not None:
            raise ValueError("Cloud reset disabled. Launch a new instance for a fresh rehearsal.")
        clients = self.clients or {
            product: httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=90,
                                  headers={"X-Agency": AGENCY}) for product, port in PORTS.items()}
        clocks = []
        for product, client in clients.items():
            health = client.get("/health")
            health.raise_for_status()
            if health.json()["instance"] != self.instance:
                raise ValueError(f"{product} instance mismatch")
            response = client.get("/sim/clock")
            response.raise_for_status()
            clocks.append(response.json()["now"])
        if len(set(clocks)) != 1:
            raise ValueError("Product clocks disagree; align the instance before starting")
        mocks = Mocks(SimClock(parse_ts(clocks[0])), clients=clients)
        self.mocks = mocks
        root = self.data_dir or BASE / self.instance / "orchestrator"
        self.system = System(AGENCY, mocks, root, reasoner=self.reasoner, bus="sqlite", db="sqlite")
        self.system.recover()
        self.system.run_until_idle()

    def _docs(self, product, coll):
        response = self.mocks.clients[product].get(f"/sim/records/{coll}", headers=self._h)
        response.raise_for_status()
        return response.json()

    def _drop_data_dir(self):
        pass  # cloud profile deliberately preserves local checkpoints, audit, kill switch and idempotency

    def sync_clocks(self):
        target = self.system.clock.now()
        for client in self.mocks.clients.values():
            response = client.get("/sim/clock", headers=self._h)
            response.raise_for_status()
            delta = (target - parse_ts(response.json()["now"])).total_seconds() / 3600
            if delta < 0:
                raise ValueError("Cannot move product clock backwards")
            if delta:
                client.post("/sim/advance", headers=self._h, json={"hours": delta}).raise_for_status()

    def advance_clock(self, hours):
        self.system.clock.advance(hours=hours)
        self.sync_clocks()
        self.system.run_until_idle()

    def close_work_order(self, run_id):
        self.sync_clocks()
        run = self.system.gateway.get_run(run_id)
        if not run or not run.wo_id:
            raise ValueError("No work order for this incident")
        self.mocks.clients["maintenance"].post(f"/sim/workorders/{run.wo_id}/close", headers=self._h).raise_for_status()
        self.system.run_until_idle()

    def take_spare(self, run_id):
        run = self.system.gateway.get_run(run_id)
        spare = next(step.params["to_vehicle_id"] for step in run.plan.steps if step.action == "reassign_block")
        native = self.system.xw.all_ids(AGENCY, spare)["scheduling"]
        self.mocks.clients["scheduling"].post("/sim/vehicle-status", headers=self._h,
            json={"vehicle": native, "status": "reserved", "changed_by": "dispatcher"}).raise_for_status()


class ConnectedSession(DemoSession):
    def validate(self, command: Command):
        if command.action == "reset":
            raise HTTPException(409, "Cloud reset disabled. Launch a new named instance for another rehearsal.")
        return super().validate(command)

    def close(self):
        super().close()
        for client in self.world.mocks.clients.values():
            client.close()


def connected_console(instance: str, reasoner=None, clients=None, data_dir=None):
    root = data_dir or BASE / instance / "orchestrator"
    memory = IncidentMemory(root / "memory.sqlite")
    session = ConnectedSession(reasoner, memory,
        world_factory=lambda **kwargs: ConnectedWorld(instance=instance, clients=clients, data_dir=root, **kwargs),
        agency=AGENCY)
    return create_app(session=session)
