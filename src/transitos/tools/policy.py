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

from transitos.clearance.evaluate import vehicle_items
from transitos.clearance.facts import Facts, native_profile
from transitos.clearance.rules import (
    day_hours_item,
    guarded,
    last_spare_item,
    lift_training_item,
)
from transitos.contracts.objects import Block, Needs, Operator, Vehicle
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
# Thin wrappers over `transitos.clearance` (Phase 10: one rule implementation). They return violation
# strings (empty list == allowed) with the shapes callers already match on.

_EPOCH = datetime(1970, 1, 1)


def check_min_spares(ready_spares_before: int, limits: Limits) -> list[str]:
    """Using one spare must leave at least min_spares ready (never pull the last spare)."""
    it = last_spare_item(ready_spares_before, 1, limits.min_spares)
    return [it.detail] if it.status == "violation" else []


def check_operator_hours(hours_today: float, block_start: datetime, block_end: datetime,
                         limits: Limits) -> list[str]:
    bh = (block_end - block_start).total_seconds() / 3600.0
    it = day_hours_item("operator", hours_today, bh, limits.max_operator_hours_day)
    return [it.detail] if it.status == "violation" else []


def check_lift_qualification(needs: Needs, operator: Operator) -> list[str]:
    if not needs.lift:
        return []
    it = lift_training_item(operator, "native")
    return [it.detail] if it.status == "violation" else []


def check_spare_meets_needs(vehicle: Vehicle, needs: Needs, policy: AgencyPolicy | None = None) -> list[str]:
    """Capacity, lift and graded type fit (a larger class passes with a warning, smaller never)."""
    block = Block(id="-", vehicle_id="-", start=_EPOCH, end=_EPOCH, needs=needs)
    facts = Facts(blocks={}, vehicles={}, operators={}, ready_count=None, commitments=None, ready_unknown=0,
                  profiles={"scheduling": native_profile("scheduling")})   # no spare count here (R5-2)
    items = guarded(f"vehicle {vehicle.id}", vehicle_items, block, vehicle, facts, policy or AgencyPolicy(agency="-"))
    return [i.detail for i in items if i.status == "violation" and i.rule != "vehicle_ready"]


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
