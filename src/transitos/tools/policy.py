"""Policy loading, live kill switch, and pure hard-limit checks (design §8).

Hard limits are code, never model output. The check_* functions are pure: they return a list of
violation strings (empty list == allowed)."""
from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

import yaml

from transitos.contracts.objects import Needs, Operator, Vehicle
from transitos.contracts.policy import AgencyPolicy, Limits

CONFIG_DIR = Path(__file__).parent / "config"


def load_policy(agency: str, config_dir: Path | None = None) -> AgencyPolicy:
    """Load config/policy/<agency>.yaml; absent file -> default (approve-everything) policy."""
    path = (config_dir or CONFIG_DIR) / "policy" / f"{agency}.yaml"
    if not path.exists():
        return AgencyPolicy(agency=agency)
    data = yaml.safe_load(path.read_text()) or {}
    data.setdefault("agency", agency)
    return AgencyPolicy.model_validate(data)


class KillSwitchStore:
    """SQLite flag per agency, toggled by the panel, read live on every write."""

    def __init__(self, db_path: str | Path = ":memory:"):
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=5)
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS kill_switch(agency TEXT PRIMARY KEY, on_flag INTEGER NOT NULL)")

    def set(self, agency: str, on: bool) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO kill_switch(agency,on_flag) VALUES(?,?) "
                "ON CONFLICT(agency) DO UPDATE SET on_flag=excluded.on_flag", (agency, int(on)))

    def get(self, agency: str) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT on_flag FROM kill_switch WHERE agency=?", (agency,)).fetchone()
        return bool(row[0]) if row else False


_default_store: KillSwitchStore | None = None


def default_store() -> KillSwitchStore:
    """Process-wide store; file-backed if TRANSITOS_KILLSWITCH_DB is set (so panel and worker share it)."""
    global _default_store
    if _default_store is None:
        _default_store = KillSwitchStore(os.environ.get("TRANSITOS_KILLSWITCH_DB", ":memory:"))
    return _default_store


def kill_switch(agency: str, store: KillSwitchStore | None = None,
                policy: AgencyPolicy | None = None) -> bool:
    """True if writes are blocked. Live read: SQLite flag OR policy-file flag (re-read each call)."""
    if (store or default_store()).get(agency):
        return True
    return (policy or load_policy(agency)).kill_switch


# ---- pure hard-limit checks -------------------------------------------------------------------

def check_min_spares(ready_spares_before: int, limits: Limits) -> list[str]:
    """Using one spare must leave at least min_spares ready (never pull the last spare)."""
    remaining = ready_spares_before - 1
    if remaining < limits.min_spares:
        return [f"min_spares: {remaining} spare(s) would remain, minimum is {limits.min_spares}"]
    return []


def check_operator_hours(hours_today: float, block_start: datetime, block_end: datetime,
                         limits: Limits) -> list[str]:
    block_hours = (block_end - block_start).total_seconds() / 3600.0
    total = hours_today + block_hours
    if total > limits.max_operator_hours_day + 1e-9:
        return [(f"operator_hours: {hours_today:.1f}h today + {block_hours:.1f}h block = {total:.1f}h "
                f"> max {limits.max_operator_hours_day:g}h")]
    return []


def check_lift_qualification(needs: Needs, operator: Operator) -> list[str]:
    if needs.lift and not operator.lift_trained:
        return [f"lift_qualification: operator {operator.id} is not lift-trained but block needs a lift"]
    return []


def check_spare_meets_needs(vehicle: Vehicle, needs: Needs) -> list[str]:
    v: list[str] = []
    if vehicle.seats < needs.seats:
        v.append(f"needs: vehicle {vehicle.id} has {vehicle.seats} seats, needs {needs.seats}")
    if vehicle.wheelchair_positions < needs.wheelchair_positions:
        v.append(f"needs: vehicle {vehicle.id} has {vehicle.wheelchair_positions} wheelchair positions, "
                 f"needs {needs.wheelchair_positions}")
    if needs.lift and not vehicle.lift_ok:
        v.append(f"needs: vehicle {vehicle.id} lift not operational")
    if needs.type and vehicle.type != needs.type:
        v.append(f"needs: vehicle {vehicle.id} type {vehicle.type} != required {needs.type}")
    return v


def check_hard_limits(*, limits: Limits, needs: Needs | None = None, vehicle: Vehicle | None = None,
                      ready_spares_before: int | None = None, operator: Operator | None = None,
                      block_start: datetime | None = None, block_end: datetime | None = None) -> list[str]:
    """Run every applicable check for the supplied facts; returns all violations."""
    v: list[str] = []
    if vehicle is not None and needs is not None:
        v += check_spare_meets_needs(vehicle, needs)
    if ready_spares_before is not None:
        v += check_min_spares(ready_spares_before, limits)
    if operator is not None and block_start is not None and block_end is not None:
        v += check_operator_hours(operator.hours_today, block_start, block_end, limits)
    if operator is not None and needs is not None:
        v += check_lift_qualification(needs, operator)
    return v
