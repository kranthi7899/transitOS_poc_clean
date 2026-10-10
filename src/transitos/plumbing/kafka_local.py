"""Helpers for the project-local broker (scripts/kafka.sh calls these).

    python -m transitos.plumbing.kafka_local purge   delete every non-internal topic except the canonical three
"""
from __future__ import annotations

import sys

from .kafka_bus import _require, bootstrap_servers

CANONICAL_TOPICS = ("events.raw", "events.resolved", "events.failed")


def purge_namespaces(bootstrap: str | None = None, timeout: float = 30.0) -> int:
    """Delete test/scenario namespace topics in one batched AdminClient call. Returns how many."""
    _require()
    import confluent_kafka as ck
    from confluent_kafka.admin import AdminClient
    admin = AdminClient({"bootstrap.servers": bootstrap or bootstrap_servers()})
    names = [t for t in admin.list_topics(timeout=timeout).topics
             if not t.startswith("__") and t not in CANONICAL_TOPICS]
    if names:
        for f in admin.delete_topics(names, operation_timeout=timeout).values():
            try:
                f.result(timeout=timeout)
            except ck.KafkaException:
                pass
    return len(names)


def main(argv: list[str]) -> int:
    if argv != ["purge"]:
        print("usage: python -m transitos.plumbing.kafka_local purge", file=sys.stderr)
        return 2
    print(f"deleted {purge_namespaces()} non-canonical topics")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
