"""Consume-style event handling for the workflow (design §2.3, §7). Wired to events.resolved by Track B/F.

`handle_event(event, runs_index, graph)`:
  - dedupe by event id (replays create no new runs)
  - START_TYPES: start a run, or attach as context if the vehicle already has an active run (A9)
  - RESUME_TYPES: resume the run waiting for repair (or park the resume if the run has not got there yet)
  - UPDATE_TYPES: attach as context and invalidate a plan that is still awaiting approval
The graph is invoked synchronously and returns at the next interrupt / end.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from langgraph.types import Command

from transitos.contracts.envelope import (
    RESUME_TYPES,
    START_TYPES,
    UPDATE_TYPES,
    CloudEvent,
)
from transitos.contracts.events import caused_by_us, resolved_vehicle_id
from transitos.contracts.ids import run_id as make_run_id
from transitos.contracts.objects import RunState
from transitos.workflow.graph import initial_state, run_config
from transitos.workflow.store import RunsIndex


@dataclass
class HandleResult:
    action: str  # started | attached | duplicate | resumed | parked | signaled | ignored | unresolved
    run_id: str | None = None


def vehicle_id_of(event: CloudEvent) -> str | None:
    """Canonical vehicle id added by the crosswalk: data["refs"]["canonical"] (contracts.events)."""
    return resolved_vehicle_id(event.data)


def _is_self_inflicted(event: CloudEvent) -> bool:
    """Events caused by our own writes must not invalidate the plan (contracts.events.caused_by_us)."""
    return caused_by_us(event.data)


def _drive(graph, run_id: str, payload) -> None:
    graph.invoke(payload, run_config(run_id))


def handle_event(event: CloudEvent, runs_index: RunsIndex, graph, clock: Callable[[], datetime] | None = None
                 ) -> HandleResult:
    if runs_index.seen(event.id):
        return HandleResult("duplicate")
    vid = vehicle_id_of(event)
    ev = event.model_dump(mode="json")

    if event.type in START_TYPES:
        if not vid:
            return HandleResult("unresolved")
        rid = make_run_id(event.agencyid, vid, event.id)
        owner = runs_index.claim(event.agencyid, vid, rid, event.id)
        if owner != rid:
            runs_index.add_context(owner, ev)
            runs_index.mark_seen(event.id, owner, "attached")
            return HandleResult("attached", owner)
        cfg = run_config(rid)
        snap = graph.get_state(cfg)
        if not snap.values:  # fresh start
            run = RunState(run_id=rid, agency=event.agencyid, vehicle_id=vid, trigger_event=ev)
            _drive(graph, rid, initial_state(run))
        elif snap.next:  # redelivery after a crash: continue from the checkpoint
            _drive(graph, rid, None)
        runs_index.mark_seen(event.id, rid, "started")
        return HandleResult("started", rid)

    if event.type in RESUME_TYPES:
        run = runs_index.active_run(event.agencyid, vid) if vid else None
        if not run:
            runs_index.mark_seen(event.id, None, "ignored")
            return HandleResult("ignored")
        payload = {"type": "work_order_closed", "wo_no": event.data.get("wo_no"), "event_id": event.id}
        if run["status"] == "WAITING_FOR_REPAIR":
            _drive(graph, run["run_id"], Command(resume=payload))
            action = "resumed"
        else:
            runs_index.set_pending_resume(run["run_id"], payload)
            action = "parked"
        runs_index.mark_seen(event.id, run["run_id"], action)
        return HandleResult(action, run["run_id"])

    if event.type in UPDATE_TYPES:
        if _is_self_inflicted(event):
            runs_index.mark_seen(event.id, None, "ignored")
            return HandleResult("ignored")
        runs = [runs_index.active_run(event.agencyid, vid)] if vid else runs_index.awaiting_runs(event.agencyid)
        runs = [r for r in runs if r]
        for r in runs:
            runs_index.add_context(r["run_id"], ev)
            if r["status"] == "AWAITING_APPROVAL":
                runs_index.mark_stale(r["run_id"])
        runs_index.mark_seen(event.id, runs[0]["run_id"] if runs else None, "signaled" if runs else "ignored")
        return HandleResult("signaled" if runs else "ignored", runs[0]["run_id"] if runs else None)

    return HandleResult("ignored")


def resume_approval(graph, runs_index: RunsIndex, run_id: str, decision: str, by: str,
                    edits: dict[str, Any] | None = None, at: datetime | None = None) -> None:
    """Called by the panel/API: deliver a human decision. Carries the stale flag set by update events."""
    payload = {"decision": decision, "by": by, "edits": edits, "stale": runs_index.pop_stale(run_id)}
    if at:
        payload["at"] = at.isoformat()
    _drive(graph, run_id, Command(resume=payload))


def resume_return(graph, run_id: str, decision: str, by: str) -> None:
    """Human approval for return-to-service (when policy autonomy for it is 'approve')."""
    _drive(graph, run_id, Command(resume={"decision": decision, "by": by}))
