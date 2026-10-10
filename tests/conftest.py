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


def _kafka_skip_reason() -> str | None:
    if not kafka_available():
        return "confluent-kafka not installed (uv sync --extra kafka)"
    if os.environ.get("TRANSITOS_KAFKA"):
        return None
    if not broker_reachable(timeout=0.5):
        return "no Kafka broker on 127.0.0.1:9092 (make kafka-up) and TRANSITOS_KAFKA not set"
    return None


def pytest_collection_modifyitems(config, items):
    reason = _kafka_skip_reason()
    admin = os.environ.get("TRANSITOS_KAFKA_ADMIN") == "1"
    for item in items:
        marked = item.get_closest_marker("kafka") or item.get_closest_marker("kafka_admin")
        if reason and marked:
            item.add_marker(pytest.mark.skip(reason=reason))
        elif item.get_closest_marker("kafka_admin") and not admin:
            item.add_marker(pytest.mark.skip(reason="stops/starts the local broker; set TRANSITOS_KAFKA_ADMIN=1"))
