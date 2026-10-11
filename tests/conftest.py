"""Kafka test gating: tests marked `kafka` run only when a broker is usable.

* TRANSITOS_KAFKA set  -> Kafka tests run against it (an unreachable broker then FAILS, it is not skipped)
* otherwise            -> they run if 127.0.0.1:9092 accepts a connection and confluent-kafka is installed,
                          else they are skipped (the default `make test` / `make verify` stays broker-free)
* `kafka_admin` tests (stop/start the local broker) additionally need TRANSITOS_KAFKA_ADMIN=1.
"""
from __future__ import annotations

import os

import pytest

from transitos.plumbing.kafka_bus import broker_reachable, kafka_available
from transitos.plumbing.pg import pg_available, server_reachable


def _kafka_skip_reason() -> str | None:
    if not kafka_available():
        return "confluent-kafka not installed (uv sync --extra kafka)"
    if os.environ.get("TRANSITOS_KAFKA"):
        return None
    if not broker_reachable(timeout=0.5):
        return "no Kafka broker on 127.0.0.1:9092 (make kafka-up) and TRANSITOS_KAFKA not set"
    return None


def _kafka_modify(config, items):
    reason = _kafka_skip_reason()
    admin = os.environ.get("TRANSITOS_KAFKA_ADMIN") == "1"
    for item in items:
        marked = item.get_closest_marker("kafka") or item.get_closest_marker("kafka_admin")
        if reason and marked:
            item.add_marker(pytest.mark.skip(reason=reason))
        elif item.get_closest_marker("kafka_admin") and not admin:
            item.add_marker(pytest.mark.skip(reason="stops/starts the local broker; set TRANSITOS_KAFKA_ADMIN=1"))


def _pg_skip_reason() -> str | None:
    """Tests marked `postgres` run when psycopg is installed and a server answers on 127.0.0.1:54329
    (or TRANSITOS_PG is set, in which case an unreachable server FAILS instead of skipping)."""
    if not pg_available():
        return "psycopg not installed (uv sync --extra postgres)"
    if os.environ.get("TRANSITOS_PG"):
        return None
    if not server_reachable(timeout=0.5):
        return "no Postgres on 127.0.0.1:54329 (make postgres-up) and TRANSITOS_PG not set"
    return None


def pytest_collection_modifyitems(config, items):
    _kafka_modify(config, items)
    reason = _pg_skip_reason()
    admin = os.environ.get("TRANSITOS_PG_ADMIN") == "1"
    for item in items:
        marked = item.get_closest_marker("postgres") or item.get_closest_marker("postgres_admin")
        if reason and marked:
            item.add_marker(pytest.mark.skip(reason=reason))
        elif item.get_closest_marker("postgres_admin") and not admin:
            item.add_marker(pytest.mark.skip(reason="stops/starts the local server; set TRANSITOS_PG_ADMIN=1"))
