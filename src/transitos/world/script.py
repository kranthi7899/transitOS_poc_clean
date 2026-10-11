"""Day scripts: the beats of a simulated day, as plain data (Phase 10, Step 3.1).

A `Beat` is something that physically happens (ground truth), independent of every product. A `DayScript` is an
ordered set of beats for one agency. Scripts serialise to canonical JSON so a scripted day is byte-identical
across runs.

`Beat.vehicle` is the subject id: a canonical vehicle id (veh-3) for fault_on / fault_off / return_to_service /
dvir, and a canonical operator id (op-1) for callout.

Params by kind (all optional unless noted):
  fault_on           spn, fmi, lamps {stop, warn}, withdrawn (default True; A90), fault_id, system
  fault_off          spn, fmi, fault_id (omit both to clear every active fault of the vehicle)
  return_to_service  (none)
  dvir               defect_text, system
  callout            operator
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

Kind = Literal["fault_on", "fault_off", "return_to_service", "dvir", "callout"]
KINDS: tuple[str, ...] = ("fault_on", "fault_off", "return_to_service", "dvir", "callout")


@dataclass(frozen=True)
class Beat:
    at: datetime
    vehicle: str
    kind: Kind
    params: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"unknown beat kind {self.kind!r}; expected one of {KINDS}")
        if self.at.tzinfo is None or self.at.utcoffset() is None:
            raise ValueError("Beat.at must be timezone-aware (no naive timestamps)")
        object.__setattr__(self, "at", self.at.astimezone(UTC))
        object.__setattr__(self, "params", dict(self.params))

    def to_dict(self) -> dict[str, Any]:
        return {"at": self.at.isoformat(), "vehicle": self.vehicle, "kind": self.kind, "params": self.params}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Beat:
        return cls(datetime.fromisoformat(d["at"]), d["vehicle"], d["kind"], d.get("params") or {})


def midnight(day: datetime | date) -> datetime:
    """00:00 UTC of the given day (a date, or a timezone-aware datetime read in UTC)."""
    if isinstance(day, datetime):
        if day.tzinfo is None:
            raise ValueError("day must be timezone-aware")
        d = day.astimezone(UTC)
        return d.replace(hour=0, minute=0, second=0, microsecond=0)
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def hhmm_on(day: datetime | date, hhmm: str) -> datetime:
    """'09:41' on the given day, UTC."""
    h, m = hhmm.split(":")
    return midnight(day) + timedelta(hours=int(h), minutes=int(m))


@dataclass(frozen=True)
class DayScript:
    agency: str
    beats: tuple[Beat, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "beats", tuple(self.beats))

    @classmethod
    def example_coolant_fault(cls, day: datetime | date, vehicle: str = "veh-3", agency: str = "agency-042") -> DayScript:
        """The spec's example day: bus 3 develops a coolant temperature fault (SPN 110) at 09:40, it clears at
        10:15 and the bus returns to service at 11:30. SPN 110 FMI 0 ("far above range") is the fixture's
        pull_vehicle row; the stop lamp is on (A90). The vehicle is a parameter so a scenario can run the same
        day on its own baseline fleet."""
        d = midnight(day)
        fault = {"spn": 110, "fmi": 0, "lamps": {"stop": True, "warn": True}}
        return cls(agency, (
            Beat(d + timedelta(hours=9, minutes=40), vehicle, "fault_on", fault),
            Beat(d + timedelta(hours=10, minutes=15), vehicle, "fault_off", {"spn": 110, "fmi": 0}),
            Beat(d + timedelta(hours=11, minutes=30), vehicle, "return_to_service", {}),
        ))

    def to_dict(self) -> dict[str, Any]:
        return {"agency": self.agency, "beats": [b.to_dict() for b in self.beats]}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, s: str) -> DayScript:
        d = json.loads(s)
        return cls(d["agency"], tuple(Beat.from_dict(b) for b in d["beats"]))


def ordered(beats: Iterable[Beat]) -> list[Beat]:
    """Time order; ties keep the script's own order (stable)."""
    return sorted(beats, key=lambda b: b.at)
