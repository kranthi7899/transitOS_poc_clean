"""Deterministic synthetic fleet. Every mock slices the same Fleet, so cross-product IDs line up
only through the crosswalk (native IDs differ per product and per agency)."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from .clock import iso


@dataclass(frozen=True)
class AgencyCfg:
    sched: str
    sched_off: int
    tele: str
    tele_off: int
    maint: str
    maint_off: int
    op: str
    op_off: int
    depots: tuple[str, str]


AGENCIES: dict[str, AgencyCfg] = {
    # veh-1 -> BUS-44 / T-77 / 1106 / (operators OP-310...)
    "agency-042": AgencyCfg("BUS-{n}", 43, "T-{n}", 76, "{n}", 1105, "OP-{n}", 309, ("North", "South")),
    # thin copy with different ID formats
    "agency-077": AgencyCfg("V{n:03d}", 100, "SM-{n}", 5000, "A{n}", 2000, "E{n}", 700, ("Main", "Annex")),
}

# idx, kind, seats, wheelchair_positions, lift_ok, role
_SPECS = [
    (1, "standard", 32, 2, True, "line"),
    (2, "standard", 32, 2, True, "line"),
    (3, "standard", 32, 2, True, "line"),  # PM nearly overdue
    (4, "standard", 36, 0, False, "line"),
    (5, "cutaway", 16, 2, True, "line"),
    (6, "cutaway", 16, 2, True, "line"),
    (7, "standard", 32, 2, True, "line"),
    (8, "articulated", 55, 2, True, "line"),
    (9, "standard", 36, 0, False, "line"),
    (10, "standard", 32, 2, True, "spare"),  # suitable for lift blocks
    (11, "cutaway", 16, 0, False, "spare"),  # small, no lift
    (12, "standard", 36, 0, False, "spare"),  # no lift
]
_SERVICE_RANK = {"cutaway": 0, "standard": 1, "articulated": 2}

# hours_today, hours_week, avail_from, avail_to, extra_board, lift_trained, quals
_OPS = [
    (4.0, 22.0, "05:00", "14:00", False, True, "CDL-B;PAX;ARTIC"),
    (6.5, 31.0, "05:00", "15:00", False, True, "CDL-B;PAX"),
    (9.5, 44.0, "05:00", "15:00", False, True, "CDL-B;PAX"),  # near the daily limit
    (3.0, 18.0, "05:00", "13:00", False, False, "CDL-B;PAX"),
    (8.0, 38.0, "06:00", "16:00", False, True, "CDL-B;PAX;ARTIC"),
    (7.0, 35.0, "12:00", "21:00", False, True, "CDL-B;PAX"),
    (5.5, 27.0, "12:00", "21:00", False, False, "CDL-B;PAX"),
    (9.0, 41.0, "12:00", "22:00", False, True, "CDL-B;PAX"),
    (2.0, 12.0, "12:00", "21:00", False, True, "CDL-B;PAX"),
    (0.0, 8.0, "05:00", "21:00", True, True, "CDL-B;PAX;ARTIC"),  # extra board
]


@dataclass
class Fleet:
    agency: str
    vehicles: list[dict[str, Any]] = field(default_factory=list)
    blocks: list[dict[str, Any]] = field(default_factory=list)
    operators: list[dict[str, Any]] = field(default_factory=list)
    pm: list[dict[str, Any]] = field(default_factory=list)


FleetBuilder = Callable[[datetime], Fleet]
_CUSTOM: dict[str, FleetBuilder] = {}


def register_fleet(agency: str, builder: FleetBuilder | None) -> None:
    """Register (or with None remove) a custom synthetic fleet for an agency id, e.g. a verification
    scenario baseline. /sim/seed and crosswalk_entries then use it. Process-wide registry."""
    if builder is None:
        _CUSTOM.pop(agency, None)
    else:
        _CUSTOM[agency] = builder


def known_agency(agency: str) -> bool:
    return agency in AGENCIES or agency in _CUSTOM


def build_fleet(agency: str, now: datetime) -> Fleet:
    if agency in _CUSTOM:
        return _CUSTOM[agency](now)
    cfg = AGENCIES[agency]
    day = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    f = Fleet(agency)

    for i, (hours_today, hours_week, a_from, a_to, extra, lift, quals) in enumerate(_OPS, start=1):
        f.operators.append({
            "idx": i, "canonical": f"op-{i}", "native": cfg.op.format(n=cfg.op_off + i),
            "qualifications": quals, "lift_trained": lift, "hours_today": hours_today,
            "hours_week": hours_week, "available_from": a_from, "available_to": a_to,
            "on_extra_board": extra,
        })

    for n, kind, seats, wc, lift, role in _SPECS:
        f.vehicles.append({
            "idx": n, "canonical": f"veh-{n}", "role": role,
            "sched": cfg.sched.format(n=cfg.sched_off + n),
            "tele": cfg.tele.format(n=cfg.tele_off + n),
            "maint": cfg.maint.format(n=cfg.maint_off + n),
            "name": f"Bus {cfg.sched_off + n}", "type": kind, "seats": seats,
            "wheelchair_positions": wc, "lift_ok": lift,
            "depot": cfg.depots[n % 2], "status": "ready" if role == "spare" else "in_service",
        })
        meter = 20000 + 1700 * n
        due = meter + 3000 + 250 * n
        if n == 3:
            meter, due = 24900, 25000  # 100 mi from overdue
        f.pm.append({"vehicle_idx": n, "service_code": "PM-A", "due_mi": due, "meter_mi": meter})

    ops = f.operators
    for v in f.vehicles:
        if v["role"] != "line":
            continue
        i = v["idx"]
        for k in range(8):
            start = day + timedelta(hours=5 + 2 * k)
            wc_need = v["wheelchair_positions"] > 0 and k % 3 == 0
            op = ops[(i - 1) % 10] if k < 4 else ops[(i + 4) % 10]
            f.blocks.append({
                "id": f"BLK-{i}-{k + 1}", "vehicle_idx": i, "vehicle": v["sched"],
                "operator": op["native"], "start": iso(start), "end": iso(start + timedelta(hours=2)),
                "start_depot": v["depot"],
                "needs": {
                    "seats": 12 if v["type"] == "cutaway" else (40 if v["type"] == "articulated" else 20),
                    "wheelchair_positions": 1 if wc_need else 0,
                    "lift": wc_need,
                    "type": v["type"],
                },
            })
    return f


def type_ok(have: str, need: str | None) -> bool:
    return need is None or _SERVICE_RANK[have] >= _SERVICE_RANK[need]
