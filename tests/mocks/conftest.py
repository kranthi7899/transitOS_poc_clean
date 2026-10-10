import pytest
from fastapi.testclient import TestClient

from transitos.mocks import alerts, maintenance, scheduling, telematics, workforce
from transitos.mocks.chaos import Chaos
from transitos.mocks.clock import SimClock

A = {"X-Agency": "agency-042"}
MODS = {"scheduling": scheduling, "telematics": telematics, "maintenance": maintenance,
        "workforce": workforce, "alerts": alerts}


@pytest.fixture
def clock():
    return SimClock()


@pytest.fixture
def make(clock):
    """make(name, agency=...) -> (client, chaos); seeded."""
    def _make(name, agency="agency-042", **kw):
        chaos = Chaos()
        c = TestClient(MODS[name].create_app(clock, chaos, None, **kw))
        assert c.post("/sim/seed", headers={"X-Agency": agency}).status_code == 200
        return c, chaos
    return _make


@pytest.fixture
def sched(make):
    return make("scheduling")


@pytest.fixture
def maint(make):
    return make("maintenance")
