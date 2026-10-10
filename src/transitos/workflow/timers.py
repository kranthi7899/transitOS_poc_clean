"""Approval timeout on the (sim) clock. Call `expire_approvals` periodically / after advancing the sim clock."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from langgraph.types import Command

from transitos.workflow.graph import run_config
from transitos.workflow.store import RunsIndex


def expire_approvals(graph, runs_index: RunsIndex, clock: Callable[[], datetime]) -> list[str]:
    """Resume every run whose approval deadline has passed with decision=expired (-> escalate)."""
    now = clock()
    expired: list[str] = []
    for r in runs_index.awaiting_runs():
        if r["deadline"] and datetime.fromisoformat(r["deadline"]) <= now:
            graph.invoke(Command(resume={"decision": "expired", "by": "timer", "at": now.isoformat(),
                                         "stale": runs_index.pop_stale(r["run_id"])}),
                         run_config(r["run_id"]))
            expired.append(r["run_id"])
    return expired
