"""Agency-scoped episodic history, never policy or live product facts (A203)."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from transitos.contracts.objects import RunState
from transitos.verification.model import Observation

ROOT = Path(__file__).resolve().parents[3]


class IncidentMemory:
    """Store only successful, separately-approved, simulated incidents; idempotent per world/run."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or ROOT / ".local" / "agent-memory.sqlite")

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            c.execute("""CREATE TABLE IF NOT EXISTS episodes (
                id INTEGER PRIMARY KEY, agency TEXT NOT NULL, vehicle TEXT NOT NULL,
                source TEXT NOT NULL, summary TEXT NOT NULL, UNIQUE(agency,source))""")
            c.commit()
            return c
        except sqlite3.Error:
            c.close()
            raise

    def remember(self, run: RunState, observation: Observation, generation: str) -> bool:
        from transitos.verification.invariants import check_all
        scopes = {a.data.get("scope") for a in observation.audit if a.kind == "approval" and a.run_id == run.run_id}
        if run.status != "DONE" or scopes != {"plan", "return_to_service"} or check_all(observation):
            return False
        if observation.agency != run.agency:
            return False
        if not run.plan or not any(r.run_id == run.run_id and r.status == "DONE" for r in observation.runs):
            return False
        actions = [w.action for w in observation.writes if w.run_id == run.run_id and w.ok and not w.compensation]
        if "set_vehicle_available" not in actions:
            return False
        summary = {"run_id": run.run_id, "outcome": "DONE", "trigger_type": run.trigger_event.get("type"),
                   "actions": actions, "approved_scopes": sorted(scopes), "simulated": True,
                   "lesson": "The approved response completed; current product facts must be checked again."}
        try:
            with closing(self._connect()) as c:
                cursor = c.execute("INSERT OR IGNORE INTO episodes(agency,vehicle,source,summary) VALUES (?,?,?,?)",
                    (run.agency, run.vehicle_id, f"{generation}:{run.run_id}", json.dumps(summary)))
                c.commit()
                return bool(cursor.rowcount)
        except (sqlite3.Error, OSError):
            return False  # memory failure cannot block or authorize the workflow

    def recall(self, agency: str, vehicle: str, limit: int = 3) -> list[dict]:
        try:
            with closing(self._connect()) as c:
                rows = c.execute("SELECT summary FROM episodes WHERE agency=? AND vehicle=? ORDER BY id DESC LIMIT ?",
                                 (agency, vehicle, min(max(limit, 0), 3))).fetchall()
            return [json.loads(row["summary"]) for row in rows]
        except (sqlite3.Error, OSError, ValueError):
            return []


class MemoryReasoner:
    """Add bounded, structured history to a proposal; the existing validator remains authoritative."""

    def __init__(self, reasoner, memory: IncidentMemory, agency: str):
        self.base, self.memory, self.agency = reasoner, memory, agency
        self.name, self.prompt_version = reasoner.name, reasoner.prompt_version
        self.budget = getattr(reasoner, "budget", None)

    def propose(self, facts: dict, attempt: int = 0) -> dict:
        vehicle = (facts.get("vehicle") or {}).get("id")
        history = self.memory.recall(self.agency, vehicle) if vehicle else []
        # Raw audit/vendor text, operator identities and past assignment IDs never enter the prompt.
        safe = [{"outcome": "DONE", "trigger_type": row.get("trigger_type"),
                 "approved_scopes": row.get("approved_scopes"), "simulated": True}
                for row in history if row.get("outcome") == "DONE" and row.get("simulated") is True]
        return self.base.propose({**facts, "prior_incidents": safe}, attempt)
