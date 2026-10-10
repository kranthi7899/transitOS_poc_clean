"""Real Track A mock apps (in-process TestClient) for cross-track integration tests."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from transitos.mocks import alerts, maintenance, scheduling, telematics, workforce
from transitos.mocks.chaos import Chaos
from transitos.mocks.clock import SimClock

AG = "agency-042"
H = {"X-Agency": AG}
MODS = {"scheduling": scheduling, "telematics": telematics, "maintenance": maintenance,
        "workforce": workforce, "alerts": alerts}


class Mocks:
    def __init__(self) -> None:
        self.clock = SimClock()
        self.chaos = {n: Chaos() for n in MODS}
        self.sent: list[tuple[str, bytes, dict]] = []   # telematics webhook deliveries (sender seam)
        self.sender = lambda url, body, headers: self.sent.append((url, body, headers))
        self.apps = {}
        for n, m in MODS.items():
            kw = {"sender": lambda url, body, headers: self.sender(url, body, headers)} if n == "telematics" else {}
            self.apps[n] = m.create_app(self.clock, self.chaos[n], None, **kw)
        self.clients = {n: TestClient(a, base_url=f"http://{n}") for n, a in self.apps.items()}
        for c in self.clients.values():
            assert c.post("/sim/seed", headers=H).status_code == 200

    def c(self, name: str) -> TestClient:
        return self.clients[name]


@pytest.fixture
def mocks() -> Mocks:
    return Mocks()
