"""Real implementations of the panel ports (Track E) over Track B (bus, crosswalk, audit), Track C
(kill switch) and Track D (graph, runs index, starter)."""
from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from transitos.contracts.envelope import TOPIC_RAW, CloudEvent
from transitos.contracts.events import PUBLISHER
from transitos.contracts.objects import DecisionRecord, RunState
from transitos.panel.ports import DecisionConflict, RunNotFound, StaleVersion
from transitos.plumbing import outbox
from transitos.plumbing.audit import DecisionStore
from transitos.plumbing.crosswalk_service import NATIVE_FIELD
from transitos.tools.policy import KillSwitchStore
from transitos.workflow.graph import run_config
from transitos.workflow.starter import resume_approval, resume_return
from transitos.workflow.store import RunsIndex

if TYPE_CHECKING:
    from .system import System


class AuditedRunsIndex(RunsIndex):
    """Track D's run index, with every DecisionRecord revision also appended to Track B's append-only
    audit store (UPDATE/DELETE blocked by triggers). D keeps its mutable latest copy for the panel."""

    def __init__(self, path: str, audit: DecisionStore):
        super().__init__(path)
        self.audit = audit

    def save_decision(self, run_id: str, plan_version: int, record: dict) -> None:
        super().save_decision(run_id, plan_version, record)
        self.audit.record_decision(DecisionRecord.model_validate(record))


class GraphRunGateway:
    """RunGateway: list/get runs from the LangGraph checkpoint; decisions resume the interrupt."""

    def __init__(self, system: System):
        self.s = system

    def _snap(self, run_id: str):
        return self.s.graph.get_state(run_config(run_id))

    def get_run(self, run_id: str) -> RunState | None:
        if self.s.store.get_run(run_id) is None:
            return None
        values = self._snap(run_id).values
        return RunState.model_validate(values["run"]) if values else None

    def list_pending(self, agency: str | None = None) -> list[RunState]:
        out = []
        for r in self.s.store.list_runs():
            if r["active"] and r["status"] in ("AWAITING_APPROVAL", "RETURNING") and agency in (None, r["agency"]):
                run = self.get_run(r["run_id"])
                if run is not None:
                    out.append(run)
        return out

    def submit_decision(self, run_id: str, decision: str, plan_version: int,
                        edits: dict[str, Any] | None, user: str) -> RunState:
        run = self.get_run(run_id)
        if run is None:
            raise RunNotFound(run_id)
        if "await_approval" not in (self._snap(run_id).next or ()):
            raise DecisionConflict(f"run {run_id} is not awaiting plan approval (status {run.status})")
        if plan_version != run.plan_version:
            raise StaleVersion(f"current plan_version is {run.plan_version}")
        resume_approval(self.s.graph, self.s.store, run_id, decision, user, edits, at=self.s.clock.now())
        return self.get_run(run_id)  # type: ignore[return-value]

    def submit_return(self, run_id: str, decision: str, user: str) -> RunState:
        run = self.get_run(run_id)
        if run is None:
            raise RunNotFound(run_id)
        if "return_gate" not in (self._snap(run_id).next or ()):
            raise DecisionConflict(f"run {run_id} is not awaiting return-to-service approval")
        resume_return(self.s.graph, run_id, decision, user)
        return self.get_run(run_id)  # type: ignore[return-value]


class OutboxEventSink:
    """EventSink: panel-created / republished events go to events.raw through the transactional outbox
    (fresh dedupe id, so a republished parked event is not swallowed by its own earlier outbox row)."""

    def __init__(self, system: System):
        self.s = system

    def publish(self, event: CloudEvent) -> None:
        field, _ = NATIVE_FIELD[event.type]
        key = f"{PUBLISHER[event.type]}:{event.data.get(field, '')}"
        with self.s.db.tx() as c:
            outbox.add(c, event, TOPIC_RAW, key, dedupe_id=f"panel:{event.id}:{uuid.uuid4().hex[:8]}")


class StoreAuditReader:
    """AuditReader over Track B's append-only DecisionStore (+ the resolved-event audit table)."""

    def __init__(self, system: System):
        self.s = system

    def for_run(self, run_id: str) -> list[DecisionRecord]:
        latest: dict[int, DecisionRecord] = {}
        for rec in self.s.decisions.list_for_run(run_id):  # revisions, latest last
            latest[rec.plan_version] = rec
        return [latest[v] for v in sorted(latest)]

    def timeline(self, agency: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for r in self.s.db.execute("SELECT payload FROM audit_resolved ORDER BY seq").fetchall():
            ev = CloudEvent.model_validate_json(r["payload"])
            if agency in (None, ev.agencyid):
                items.append({"time": ev.time.isoformat(), "run_id": None, "kind": ev.type, "agency": ev.agencyid,
                              "summary": f"{ev.id} -> {ev.canonicalid}"})
        runs = {r["run_id"]: r for r in self.s.store.list_runs()}
        for rid, r in runs.items():
            if agency not in (None, r["agency"]):
                continue
            for rec in self.for_run(rid):
                for a in rec.actions:
                    items.append({"time": a.get("at", ""), "run_id": rid, "kind": a.get("kind", "write"),
                                  "agency": r["agency"], "summary": f"{a.get('step')} {a.get('status')} key={a.get('key')}"})
                if rec.outcome != "pending":
                    items.append({"time": "", "run_id": rid, "kind": f"outcome:{rec.outcome}", "agency": r["agency"],
                                  "summary": f"plan v{rec.plan_version}"})
        items.sort(key=lambda i: i["time"] or "~")
        return items[-limit:]


class KillSwitchPort:
    """Panel KillSwitchStore port over Track C's SQLite kill switch (read live by adapters + workflow)."""

    def __init__(self, store: KillSwitchStore, on_set: Callable[[str, bool], None] | None = None):
        self.store, self.on_set = store, on_set

    def is_on(self, agency: str) -> bool:
        return self.store.get(agency)

    def set(self, agency: str, on: bool) -> None:
        self.store.set(agency, on)
        if self.on_set:
            self.on_set(agency, on)


class CrosswalkUnresolved:
    """UnresolvedReader over SqliteCrosswalk's parked table."""

    def __init__(self, system: System):
        self.s = system

    def list_unresolved(self, agency: str | None = None) -> list[dict[str, Any]]:
        grouped: dict[tuple[str, str, str], list[str]] = defaultdict(list)
        for r in self.s.xw.unresolved(agency):
            grouped[(r["agency"], r["product"], r["native_id"])].append(r["event_id"])
        return [{"agency": a, "product": p, "native_id": n, "event_ids": e} for (a, p, n), e in grouped.items()]
