"""In-memory fakes for the panel ports."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from transitos.contracts.crosswalk import CrosswalkEntry
from transitos.contracts.envelope import CloudEvent
from transitos.contracts.objects import DecisionRecord, RunState

from .ports import DecisionConflict, RunNotFound, StaleVersion

PENDING_STATUS = "AWAITING_APPROVAL"


class FakeRunGateway:
    def __init__(self, runs: list[RunState] | None = None) -> None:
        self.runs = {r.run_id: r for r in runs or []}
        self.resume_calls: list[tuple[str, str, int, dict | None, str]] = []

    def list_pending(self, agency: str | None = None) -> list[RunState]:
        return [r for r in self.runs.values()
                if r.status == PENDING_STATUS and (agency is None or r.agency == agency)]

    def get_run(self, run_id: str) -> RunState | None:
        return self.runs.get(run_id)

    def submit_decision(self, run_id, decision, plan_version, edits, user, acknowledged=None):
        run = self.runs.get(run_id)
        if run is None:
            raise RunNotFound(run_id)
        if run.decision is not None:
            if run.decision == decision and run.plan_version == plan_version:
                return run  # idempotent: no second resume
            raise DecisionConflict(run_id)
        if plan_version != run.plan_version:
            raise StaleVersion(run_id)
        self.resume_calls.append((run_id, decision, plan_version, edits, user))
        run.acknowledged = sorted(set(run.acknowledged) | set(acknowledged or []))
        run.decision = decision
        run.decided_by = user
        run.decided_at = datetime.now(UTC)
        run.status = {"approve": "EXECUTING", "reject": "CLOSED_REJECTED",
                      "edit": "REVALIDATING"}.get(decision, run.status)
        return run


    def submit_return(self, run_id, decision, user):
        run = self.runs.get(run_id)
        if run is None:
            raise RunNotFound(run_id)
        if run.status != "RETURNING":
            raise DecisionConflict(run_id)
        self.resume_calls.append((run_id, f"return:{decision}", run.plan_version, None, user))
        run.status = "DONE" if decision == "approve" else "ESCALATED"
        return run


class FakeEventSink:
    def __init__(self) -> None:
        self.events: list[CloudEvent] = []

    def publish(self, event: CloudEvent) -> None:
        self.events.append(event)


class FakeCrosswalk:
    """Implements the Crosswalk protocol plus the UnresolvedReader port."""

    def __init__(self) -> None:
        self.map: dict[tuple[str, str, str], str] = {}
        self.parked: dict[tuple[str, str, str], list[CloudEvent]] = {}
        self.unresolved_ids: dict[tuple[str, str, str], list[str]] = {}

    def lookup(self, agency, product, native_id):
        return self.map.get((agency, product, native_id))

    def all_ids(self, agency, canonical_id, at=None, entities=None):
        """A test double with no validity or entity: `at` and `entities` are accepted and ignored (A147)."""
        return {p: n for (a, p, n), c in self.map.items() if a == agency and c == canonical_id}

    def add_unresolved(self, agency, product, native_id, event_id):
        self.unresolved_ids.setdefault((agency, product, native_id), []).append(event_id)

    def park(self, event: CloudEvent, product: str, native_id: str) -> None:
        key = (event.agencyid, product, native_id)
        self.parked.setdefault(key, []).append(event)
        self.add_unresolved(event.agencyid, product, native_id, event.id)

    def resolve(self, entry: CrosswalkEntry) -> list[CloudEvent]:
        key = (entry.agency, entry.product, entry.native_id)
        self.map[key] = entry.canonical_id
        self.unresolved_ids.pop(key, None)
        return self.parked.pop(key, [])

    def list_unresolved(self, agency=None):
        return [{"agency": a, "product": p, "native_id": n, "event_ids": ids}
                for (a, p, n), ids in self.unresolved_ids.items() if agency in (None, a)]


class FakeAuditReader:
    def __init__(self) -> None:
        self.records: list[DecisionRecord] = []
        self.entries: list[dict[str, Any]] = []

    def add(self, rec: DecisionRecord) -> None:
        self.records.append(rec)

    def for_run(self, run_id):
        return [r for r in self.records if r.run_id == run_id]

    def timeline(self, agency=None, limit=100):
        items = [e for e in self.entries if agency in (None, e.get("agency"))]
        return items[-limit:]


class FakeKillSwitch:
    def __init__(self) -> None:
        self.state: dict[str, bool] = {}

    def is_on(self, agency):
        return self.state.get(agency, False)

    def set(self, agency, on):
        self.state[agency] = on
