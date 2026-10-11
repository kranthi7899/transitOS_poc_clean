"""Detection latency (Phase 10, Step 3.4): how long from the physical fault to each stage of the orchestrator's
reaction, in SIMULATED seconds (the sim clock, never wall time).

  truth_to_telematics_s       fault truth -> telematics first showed it          (from the world engine's log)
  truth_to_first_proposal_s   fault truth -> the first plan proposed by a run    (audit `proposal`, data["at"])
  truth_to_pull_s             fault truth -> the first successful, non-compensating `set_vehicle_unavailable`
                              write (audit `write`, data["action"], data["at"]): the vehicle is pulled

`audit` is the list of `AuditEvent`s the integrated world stamps with the sim time it saw them (see
IntegrationWorld.latency_audit; A99). A stage that did not happen, or cannot be measured, is None: never 0.
Truth is the earliest fault (or DVIR) beat in the log; a proposal or write before it belongs to something else.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from transitos.mocks.clock import parse_ts
from transitos.world.engine import Observation

from .model import AuditEvent

FAULT_KINDS = ("fault_on", "dvir")
PULL_ACTION = "set_vehicle_unavailable"
KEYS = ("truth_to_telematics_s", "truth_to_first_proposal_s", "truth_to_pull_s")


def _seconds(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds()


def _first_after(audit: Iterable[AuditEvent], truth: datetime, pred) -> datetime | None:
    times = [t for e in audit if pred(e) and (t := parse_ts(e.data["at"])) >= truth]
    return min(times) if times else None


def detection_latency(obs_log: Iterable[Observation], audit: Iterable[AuditEvent]) -> dict[str, float | None]:
    obs, audit = list(obs_log), list(audit)
    faults = [o for o in obs if o.kind in FAULT_KINDS]
    if not faults:
        return dict.fromkeys(KEYS)
    truth = min(o.truth_at for o in faults)
    tele = [o.visible_at for o in faults if o.product == "telematics"]
    proposal = _first_after(audit, truth, lambda e: e.kind == "proposal" and "at" in e.data)
    pull = _first_after(audit, truth, lambda e: e.kind == "write" and "at" in e.data
                        and e.data.get("action") == PULL_ACTION and not e.data.get("compensation")
                        and e.data.get("ok", True))
    return {
        "truth_to_telematics_s": _seconds(truth, min(tele)) if tele else None,
        "truth_to_first_proposal_s": _seconds(truth, proposal) if proposal else None,
        "truth_to_pull_s": _seconds(truth, pull) if pull else None,
    }
