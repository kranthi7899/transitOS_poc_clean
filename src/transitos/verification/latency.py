"""Detection latency (Phase 10, Step 3.4; fix round): how long from the physical fault to each stage of the
orchestrator's reaction, in SIMULATED seconds (the sim clock, never wall time).

  truth_to_telematics_s       fault truth -> telematics first showed it          (the world engine's log)
  truth_to_first_proposal_s   fault truth -> the first plan proposed for that vehicle (audit `proposal`)
  truth_to_approval_s         fault truth -> the (scripted) human approval of the plan (audit `approval`)
  truth_to_pull_s             fault truth -> the first successful, non-compensating `set_vehicle_unavailable` write
  approval_to_pull_s          approval -> that write (the system's own part of the pull)

HONESTY (A99): sim time does not advance while the system processes an event, so truth_to_first_proposal_s equals the
ASSUMED telematics lag by construction, and truth_to_pull_s is dominated by the scripted human approval. Neither is a
measurement of system speed; LATENCY_NOTE says so wherever the numbers are printed.

Truth is the earliest fault_on/dvir beat PER VEHICLE (from GroundTruth.applied when given, else from the observation
log). A proposal, approval or write counts only for the SAME vehicle's run (`data["vehicle"]`) and only if it is not
before that vehicle's truth. The flat keys are those of the vehicle with the earliest truth; `per_vehicle` holds all.
A stage that did not happen, or cannot be measured, is None: never 0.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from transitos.mocks.clock import parse_ts
from transitos.world.engine import Observation
from transitos.world.script import Beat

from .model import AuditEvent

FAULT_KINDS = ("fault_on", "dvir")
PULL_ACTION = "set_vehicle_unavailable"
KEYS = ("truth_to_telematics_s", "truth_to_first_proposal_s", "truth_to_approval_s", "truth_to_pull_s",
        "approval_to_pull_s")
LATENCY_NOTE = ("Simulated seconds. truth_to_first_proposal_s equals the ASSUMED telematics lag by construction "
                "(sim time does not advance while the system processes an event). truth_to_pull_s is dominated by the "
                "scripted human approval: see truth_to_approval_s (the script's choice) and approval_to_pull_s. "
                "None of these is a measurement of system speed.")


def _seconds(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds()


def _first(audit: Iterable[AuditEvent], vehicle: str, after: datetime, pred) -> datetime | None:
    times = [t for e in audit if e.data.get("vehicle") == vehicle and "at" in e.data and pred(e)
             and (t := parse_ts(e.data["at"])) >= after]
    return min(times) if times else None


def _one(vehicle: str, truth: datetime, obs: list[Observation], audit: list[AuditEvent]) -> dict[str, float | None]:
    tele = [o.visible_at for o in obs if o.vehicle == vehicle and o.product == "telematics"
            and o.kind in FAULT_KINDS and o.truth_at == truth]
    proposal = _first(audit, vehicle, truth, lambda e: e.kind == "proposal")
    approval = _first(audit, vehicle, truth, lambda e: e.kind == "approval" and e.data.get("scope", "plan") == "plan")
    pull = _first(audit, vehicle, truth, lambda e: e.kind == "write" and e.data.get("action") == PULL_ACTION
                  and not e.data.get("compensation") and e.data.get("ok", True))
    return {
        "truth_to_telematics_s": _seconds(truth, min(tele)) if tele else None,
        "truth_to_first_proposal_s": _seconds(truth, proposal) if proposal else None,
        "truth_to_approval_s": _seconds(truth, approval) if approval else None,
        "truth_to_pull_s": _seconds(truth, pull) if pull else None,
        "approval_to_pull_s": _seconds(approval, pull) if approval and pull else None,
    }


def detection_latency(obs_log: Iterable[Observation], audit: Iterable[AuditEvent],
                      truth_beats: Iterable[Beat] | None = None) -> dict:
    obs, audit = list(obs_log), list(audit)
    truths: dict[str, datetime] = {}
    sources = ([(b.vehicle, b.at) for b in truth_beats if b.kind in FAULT_KINDS] if truth_beats is not None
               else [(o.vehicle, o.truth_at) for o in obs if o.kind in FAULT_KINDS])
    for veh, t in sources:
        if veh not in truths or t < truths[veh]:
            truths[veh] = t
    if not truths:
        return {**dict.fromkeys(KEYS), "per_vehicle": {}}
    per = {v: _one(v, truths[v], obs, audit) for v in sorted(truths)}
    first = min(truths, key=lambda v: (truths[v], v))
    return {**per[first], "per_vehicle": per}
