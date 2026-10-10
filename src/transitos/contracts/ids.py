"""Deterministic event IDs: the same business fact always yields the same ID (design §2.3)."""
from __future__ import annotations


def fault_id(agency: str, native_vehicle: str, spn: int, fmi: int, first_seen: str) -> str:
    return f"fault:{agency}:{native_vehicle}:{spn}:{fmi}:{first_seen}"


def pm_id(agency: str, asset: str, service_code: str, due_mi: int) -> str:
    return f"pm:{agency}:{asset}:{service_code}:{due_mi}"


def defect_id(agency: str, native_vehicle: str, reported_at: str, text_hash: str) -> str:
    return f"defect:{agency}:{native_vehicle}:{reported_at}:{text_hash}"


def wo_open_id(agency: str, wo_no: str) -> str:
    return f"wo-open:{agency}:{wo_no}"


def wo_close_id(agency: str, wo_no: str) -> str:
    return f"wo-close:{agency}:{wo_no}"


def vstatus_id(agency: str, vehicle: str, status: str, changed_at: str) -> str:
    return f"vstatus:{agency}:{vehicle}:{status}:{changed_at}"


def operator_avail_id(agency: str, operator: str, status: str, changed_at: str) -> str:
    return f"opavail:{agency}:{operator}:{status}:{changed_at}"


def run_id(agency: str, vehicle_id: str, trigger_event_id: str) -> str:
    return f"{agency}:{vehicle_id}:{trigger_event_id}"


def step_key(run: str, step_name: str, plan_version: int) -> str:
    """Idempotency key for a write step (design §4.5)."""
    return f"{run}:{step_name}:{plan_version}"


COMP_PREFIX = "comp:"


def compensation_key(run: str, step_name: str, plan_version: int) -> str:
    """Idempotency key of the compensation for the step `step_name` (design §4.6)."""
    return step_key(run, COMP_PREFIX + step_name, plan_version)


def compensated_key(run: str, comp_key: str) -> str | None:
    """Inverse of compensation_key: the forward step key a compensation key undoes (None if not one)."""
    rest = comp_key[len(run) + 1:] if comp_key.startswith(run + ":") else None
    if not rest or not rest.startswith(COMP_PREFIX):
        return None
    return f"{run}:{rest[len(COMP_PREFIX):]}"
