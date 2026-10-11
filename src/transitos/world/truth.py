"""Ground truth: what is physically true about each vehicle, separate from every product (Phase 10, Step 3.1).

No product reads or writes this. The world engine reads the beats it releases and shows each product its own,
delayed view (lag.py, engine.py); scenarios assert against it (for example detection latency).

State model (A90, ASSUMED): a vehicle is `ok`; an active fault makes it `faulted`; when the last active fault
clears the vehicle is `out_of_service` if the crew withdrew it (fault_on param `withdrawn`, default True: a
cleared fault still leaves the bus held for inspection) or back to `ok` if not (an intermittent fault the bus
runs through); `return_to_service` makes it `ok` again.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from transitos.mocks.clock import SimClock

from .script import Beat, DayScript

State = Literal["ok", "faulted", "out_of_service"]


def _fault_key(beat: Beat) -> str:
    p = beat.params
    return str(p["fault_id"]) if "fault_id" in p else f"{p.get('spn')}:{p.get('fmi')}"


class GroundTruth:
    """`seed` is recorded for provenance and used by the stochastic generator (stochastic.py) that builds the
    script; a hand-written script is already fully determined, so the seed changes nothing about it."""

    def __init__(self, clock: SimClock, script: DayScript, seed: int) -> None:
        self.clock, self.script, self.seed = clock, script, seed
        idx = sorted(range(len(script.beats)), key=lambda i: (script.beats[i].at, i))
        self._beats: list[Beat] = [script.beats[i] for i in idx]
        self._cursor = 0
        self._faults: dict[str, set[str]] = {}           # vehicle -> active fault keys
        self._held: set[str] = set()                     # vehicles the crew withdrew while faulted
        self._state: dict[str, State] = {}
        self.applied: list[Beat] = []

    def next_beat_at(self) -> datetime | None:
        return self._beats[self._cursor].at if self._cursor < len(self._beats) else None

    def advance_to(self, t: datetime | None = None) -> list[Beat]:
        """Release every not-yet-released beat with `at <= t` (default: the clock's now), in time order, each
        exactly once. A time before an earlier call releases nothing and never re-releases anything."""
        t = t or self.clock.now()
        out: list[Beat] = []
        while self._cursor < len(self._beats) and self._beats[self._cursor].at <= t:
            beat = self._beats[self._cursor]
            self._cursor += 1
            self._apply(beat)
            out.append(beat)
        self.applied.extend(out)
        return out

    def _apply(self, beat: Beat) -> None:
        v = beat.vehicle
        if beat.kind == "fault_on":
            self._faults.setdefault(v, set()).add(_fault_key(beat))
            if beat.params.get("withdrawn", True):
                self._held.add(v)
            self._state[v] = "faulted"
        elif beat.kind == "fault_off":
            active = self._faults.setdefault(v, set())
            if "spn" in beat.params or "fault_id" in beat.params:
                active.discard(_fault_key(beat))
            else:
                active.clear()                            # no id: every fault of the vehicle clears
            if not active and self._state.get(v, "ok") == "faulted":
                self._state[v] = "out_of_service" if v in self._held else "ok"
                self._held.discard(v)
        elif beat.kind == "return_to_service":
            self._faults.pop(v, None)
            self._held.discard(v)
            self._state[v] = "ok"
        # dvir and callout leave the vehicle's physical state alone

    def state(self, vehicle: str) -> State:
        return self._state.get(vehicle, "ok")
