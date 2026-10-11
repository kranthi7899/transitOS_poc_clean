import uuid

import pytest
from fastapi.testclient import TestClient

from transitos.plumbing import (
    CrosswalkService,
    Db,
    KafkaBus,
    Relay,
    SqliteBus,
    SqliteCrosswalk,
)

AG = "ag1"


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.postgres)])
def db(request, tmp_path):
    """Plumbing database: a SQLite file, or (when a server is up) a throwaway Postgres schema."""
    if request.param == "sqlite":
        yield Db(str(tmp_path / "t.db"))
        return
    from transitos.plumbing import pg
    schema = pg.schema_name(f"test_{uuid.uuid4().hex[:10]}", "plumbing")
    d = pg.PgDb(schema)
    yield d
    d.execute(f'DROP SCHEMA "{schema}" CASCADE')
    d.close()


def kafka_bus() -> KafkaBus:
    """A KafkaBus on its own throwaway namespace (topics + groups), so tests never see each other."""
    return KafkaBus(namespace=f"test-{uuid.uuid4().hex[:10]}")


@pytest.fixture(params=["sqlite", pytest.param("kafka", marks=pytest.mark.kafka)])
def bus(request, db):
    """Every bus-using plumbing test runs on SqliteBus and (when a broker is up) on KafkaBus."""
    if request.param == "sqlite":
        yield SqliteBus(db)
        return
    b = kafka_bus()
    yield b
    b.close()
    b.delete_topics()


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
