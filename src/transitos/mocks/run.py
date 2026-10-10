"""Serve all five mocks on ports 8101-8105: `python -m transitos.mocks.run`.
One process, one shared SimClock; each mock has its own Chaos and in-memory DB."""
from __future__ import annotations

import threading

import uvicorn
from fastapi.testclient import TestClient

from . import alerts, maintenance, scheduling, telematics, workforce
from .chaos import Chaos
from .clock import SimClock
from .seed import AGENCIES

PORTS = {"scheduling": 8101, "telematics": 8102, "maintenance": 8103, "workforce": 8104, "alerts": 8105}
MODULES = {"scheduling": scheduling, "telematics": telematics, "maintenance": maintenance,
           "workforce": workforce, "alerts": alerts}


def build_all(clock: SimClock | None = None, seed: bool = True) -> dict:
    clock = clock or SimClock()
    apps = {name: mod.create_app(clock, Chaos(), None) for name, mod in MODULES.items()}
    if seed:
        for app in apps.values():
            c = TestClient(app)
            for a in AGENCIES:
                c.post("/sim/seed", headers={"X-Agency": a})
    return apps


def main() -> None:
    apps = build_all()
    servers = [uvicorn.Server(uvicorn.Config(app, port=PORTS[n], log_level="warning")) for n, app in apps.items()]
    threads = [threading.Thread(target=s.run, daemon=True) for s in servers[1:]]
    for t in threads:
        t.start()
    print("mocks on", PORTS)
    servers[0].run()


if __name__ == "__main__":
    main()
