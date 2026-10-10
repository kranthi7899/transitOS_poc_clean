import pytest
from fastapi.testclient import TestClient

from transitos.plumbing import CrosswalkService, Db, Relay, SqliteBus, SqliteCrosswalk

AG = "ag1"


@pytest.fixture
def db(tmp_path):
    return Db(str(tmp_path / "t.db"))


@pytest.fixture
def bus(db):
    return SqliteBus(db)


@pytest.fixture
def relay(db, bus):
    return Relay(db, bus)


@pytest.fixture
def xw(db, bus):
    return SqliteCrosswalk(db, bus)


@pytest.fixture
def svc(db, bus, xw):
    return CrosswalkService(db, bus, xw)


def client(app):
    return TestClient(app, base_url="http://mock")
