"""Bug sweep B round 3 (R3 N1, A174): `WorldEngine.tick()` has a pass cap. An actor whose `discover` re-queues work
at the same instant forever makes tick() RAISE a clear error naming the actor and its waiting set, never hang."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from transitos.mocks.clock import SimClock
from transitos.world import engine as engine_mod
from transitos.world.actors import QUIET, Actor, DispatcherActor
from transitos.world.engine import TickLimitExceeded, WorldEngine
from transitos.world.lag import ProductLag
from transitos.world.ports import RecordingPorts
from transitos.world.script import Beat, DayScript
from transitos.world.truth import GroundTruth

AG = "agency-042"
NOON = datetime(2026, 10, 10, 12, tzinfo=UTC)


class ForeverRequeue(Actor):
    """Always re-queues the same beat for NOW and never makes progress."""
    product = "scheduling"

    def __init__(self) -> None:
        super().__init__(ProductLag())
        self.waiting = ["veh-3"]

    def handles(self, beat: Beat) -> bool:
        return False

    def discover(self, now, ports):
        return [(now, Beat(now, "veh-3", "return_to_service", {}))]

    def act(self, beat, ports):
        return QUIET


def test_a_discover_that_never_settles_raises_naming_the_actor_and_waiting_set():
    clock = SimClock(NOON)
    eng = WorldEngine(GroundTruth(clock, DayScript(AG, ()), seed=1), ProductLag(), RecordingPorts(clock), clock,
                      actors=[ForeverRequeue()])
    with pytest.raises(TickLimitExceeded) as ei:
        eng.tick()
    msg = str(ei.value)
    assert "ForeverRequeue" in msg and "veh-3" in msg and str(engine_mod.MAX_TICK_PASSES) in msg


def test_the_cap_is_a_named_constant_well_above_any_real_day():
    assert engine_mod.MAX_TICK_PASSES == 10_000


def test_the_dispatcher_exposes_its_waiting_set():
    d = DispatcherActor(ProductLag())
    assert d.waiting == []
