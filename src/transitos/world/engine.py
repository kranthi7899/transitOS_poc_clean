"""The world engine (Phase 10, Step 3.2): ground truth in, per-product delayed views out.

`tick()` releases the truth beats that are due at the clock's now, schedules each product's reaction at
`beat.at + that product's lag`, and performs every reaction that is due. Nothing is ever performed before
`truth + lag`. `run_until(t)` moves the SimClock to each due instant in turn (calling `settle()` after each, e.g.
`System.run_until_idle`), so a product's own timestamp equals `visible_at` exactly; a coarse `tick()` after a
long clock jump is late, never early.

Pure sim-clock code: no wall clock, no randomness, no network.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

from transitos.mocks.clock import SimClock

from .actors import QUIET, Actor, DispatcherActor, ShopActor, TelematicsActor
from .lag import ProductLag
from .ports import Ports
from .script import Beat
from .truth import GroundTruth


@dataclass(frozen=True)
class Observation:
    """A product came to hold a piece of the truth. `kind` is the truth beat it reflects."""
    product: str
    vehicle: str
    kind: str
    truth_at: datetime
    visible_at: datetime

    def to_dict(self) -> dict:
        d = asdict(self)
        d["truth_at"], d["visible_at"] = self.truth_at.isoformat(), self.visible_at.isoformat()
        return d


@dataclass(frozen=True)
class Skipped:
    """An actor stood down (not an observation): the product already held the truth, or a rule said no."""
    product: str
    vehicle: str
    kind: str
    truth_at: datetime
    at: datetime
    reason: str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["truth_at"], d["at"] = self.truth_at.isoformat(), self.at.isoformat()
        return d


@dataclass
class _Pending:
    due_at: datetime
    order: int
    actor: Actor
    beat: Beat | None
    job: Any = None      # Phase 10 Step 4.1: a watching actor's own job (ForemanActor), instead of a truth beat


class WorldEngine:
    def __init__(self, truth: GroundTruth, lag: ProductLag, ports: Ports, clock: SimClock,
                 settle: Callable[[], object] | None = None, actors: Sequence[Actor] | None = None) -> None:
        self.truth, self.lag, self.ports, self.clock, self.settle = truth, lag, ports, clock, settle
        self.actors: list[Actor] = list(actors) if actors is not None else [
            TelematicsActor(lag), ShopActor(lag), DispatcherActor(lag)]
        self.observations_log: list[Observation] = []
        self.skipped: list[Skipped] = []
        self.shop_log: list[dict] = []      # Step 4.1: what watching actors (the foreman) did, in order
        self._pending: list[_Pending] = []
        self._order = 0

    # ------------------------------------------------------------------ stepping
    def _discover(self) -> None:
        """Phase 10 Step 4.1: actors that WATCH a product (ForemanActor) schedule their own jobs. Actors without a
        `discover` method (every Step 3 actor) are untouched."""
        now = self.clock.now()
        for actor in self.actors:
            if callable(discover := getattr(actor, "discover", None)):
                for due_at, job in discover(now, self.ports):
                    self._order += 1
                    if isinstance(job, Beat):   # bug sweep B fix round (A168): a truth beat the actor acts on again
                        self._pending.append(_Pending(due_at, self._order, actor, job))
                    else:
                        self._pending.append(_Pending(due_at, self._order, actor, None, job))

    def tick(self) -> list[Observation]:
        """Release the beats due now, then perform every reaction due now, in (due time, release order)."""
        now = self.clock.now()
        for beat in self.truth.advance_to(now):
            for actor in self.actors:
                if actor.handles(beat):
                    self._order += 1
                    self._pending.append(_Pending(beat.at + actor.delay(beat), self._order, actor, beat))
        fired: list[Observation] = []
        while True:
            self._discover()      # a job may queue follow-ups due at this same instant
            due = sorted((p for p in self._pending if p.due_at <= now), key=lambda p: (p.due_at, p.order))
            if not due:
                return fired
            self._pending = [p for p in self._pending if p.due_at > now]
            fired += self._perform(due, now)

    def _perform(self, due: list[_Pending], now: datetime) -> list[Observation]:
        fired: list[Observation] = []
        for i, p in enumerate(due):
            try:
                reason = p.actor.act(p.beat, self.ports) if p.beat is not None else \
                    p.actor.perform(p.job, self.ports, now)  # type: ignore[attr-defined]
            except BaseException:
                # a port call failed: this reaction and every later due one stay pending for the next tick (the beat
                # was already released by the truth and is never released again, so nothing else would retry them)
                self._pending = due[i:] + self._pending
                raise
            if p.beat is None:   # a watching actor's job: logged apart from the truth observations
                self.shop_log.append({"at": now.isoformat(), "job": p.job.kind, "pr": p.job.pr_id,
                                      "wo": p.job.wo_id, "status": p.job.status, "skipped": reason})
                if reason is not None:
                    self.skipped.append(Skipped(p.actor.product, p.job.vehicle, f"foreman:{p.job.kind}", p.due_at,
                                                now, reason))
                continue
            if reason is QUIET:   # bug sweep B fix round (A168, A169): waiting, or not this actor's business: no row
                continue
            product = p.actor.product_of(p.beat)
            if reason is None:
                o = Observation(product, p.beat.vehicle, p.beat.kind, p.beat.at, now)
                self.observations_log.append(o)
                fired.append(o)
            else:
                self.skipped.append(Skipped(product, p.beat.vehicle, p.beat.kind, p.beat.at, now, reason))
        return fired

    def _next_instant(self, after: datetime) -> datetime | None:
        cands = [p.due_at for p in self._pending]
        if (b := self.truth.next_beat_at()) is not None:
            cands.append(b)
        cands = [max(c, after) for c in cands]
        return min(cands) if cands else None

    def _move_to(self, target: datetime) -> None:
        now = self.clock.now()
        if target <= now:
            return
        self.clock.advance(minutes=(target - now).total_seconds() / 60)
        if self.clock.now() != target:                    # float rounding of the minutes argument: close the gap
            self.clock.advance(minutes=(target - self.clock.now()).total_seconds() / 60)
        if self.clock.now() != target:
            raise RuntimeError(f"could not set the sim clock to {target.isoformat()}")

    def run_until(self, t: datetime) -> list[Observation]:
        """Advance the sim clock to `t`, stopping at every instant something is due. A `t` in the past is a no-op."""
        out: list[Observation] = []
        if t < self.clock.now():
            return out
        while True:
            self._discover()      # Step 4.1: a request made since the last step is seen now (no-op for beat actors)
            nxt = self._next_instant(self.clock.now())
            if nxt is None or nxt > t:
                break
            self._move_to(nxt)
            out += self.tick()
            if self.settle:
                self.settle()
        self._move_to(t)
        out += self.tick()
        if self.settle:
            self.settle()
        return out

    # ------------------------------------------------------------------ output
    def timeline(self) -> list[dict]:
        return [o.to_dict() for o in self.observations_log]

    def timeline_json(self) -> str:
        """Canonical serialisation of the observation log, for byte-identical comparison."""
        return json.dumps(self.timeline(), sort_keys=True, separators=(",", ":"))

    def lag_for(self, product: str) -> timedelta:
        return self.lag.delay(product)
