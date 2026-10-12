"""Bug sweep B, world engine. Expected rows are written from the ruling text, not read back from the implementation.

F9-R3 (A158): the extra board is chosen by a per-operator seeded rank (`<seed>|extra|<operator id>`), so adding an
operator changes the board only if the new operator outranks a member (it then displaces exactly one), and every
other operator's call-out timeline is unchanged (call-out streams stay per operator).

F13-R3 (A156): on a `return_to_service` beat the dispatcher flips scheduling back after its lag (12 min, ASSUMED A91)
when scheduling shows out_of_service because of THIS world's pull, so a later stop-lamp fault on the same bus is
pulled (seen) again instead of skipped."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from transitos.integration.system import Mocks
from transitos.mocks.clock import SimClock
from transitos.mocks.seed import build_fleet
from transitos.world.actors import DispatcherActor
from transitos.world.engine import WorldEngine
from transitos.world.lag import ProductLag
from transitos.world.ports import MockPorts, RecordingPorts
from transitos.world.script import Beat, DayScript
from transitos.world.stochastic import StochasticConfig, generate_world
from transitos.world.truth import GroundTruth

AG = "agency-042"
H = {"X-Agency": AG}
START = datetime(2026, 10, 10, tzinfo=UTC)
OPS10 = tuple(f"op-{n}" for n in range(1, 11))
OPS11 = (*OPS10, "op-11")


# ------------------------------------------------------------------ F9-R3: extra board by per-operator rank
def _cfg(seed, operators, days=200):
    return StochasticConfig(seed=seed, agency=AG, vehicles=(), operators=operators, start=START, days=days,
                            revenue_miles_per_day=150.0)


def _callouts(w, op):
    return [b.at for b in w.script.beats if b.kind == "callout" and b.params["operator"] == op]


@pytest.mark.parametrize("seed", range(40))
def test_adding_an_operator_changes_the_board_at_most_by_the_new_member_displacing_one(seed):
    a, b = generate_world(_cfg(seed, OPS10)), generate_world(_cfg(seed, OPS11))
    old, new = set(a.extra_board), set(b.extra_board)
    assert len(old) == len(new)          # 10 -> 11 operators: round(n*k/10) is the same for k in {1, 2}
    if new == old:
        displaced = set()
    else:
        assert "op-11" in new and len(old - new) == 1 and new == (old - (old - new)) | {"op-11"}
        displaced = old - new
    for op in OPS10:
        if op in displaced:
            continue                     # it left the board: it is now scheduled, so it may call out
        assert _callouts(a, op) == _callouts(b, op), (seed, op)


def test_the_board_is_the_top_ranked_operators_whatever_the_listing_order():
    w1 = generate_world(_cfg(7, OPS11))
    w2 = generate_world(_cfg(7, tuple(reversed(OPS11))))
    assert w1.extra_board == w2.extra_board and w1.to_json() == w2.to_json()
    sizes = {len(generate_world(_cfg(s, OPS10, days=1)).extra_board) for s in range(40)}
    assert sizes == {1, 2}                # A98 unchanged: one or two of every ten


def test_the_displacing_case_actually_happens_for_some_seed():
    """Guard against a vacuous pass: across seeds the new operator sometimes joins the board."""
    joined = [s for s in range(40) if "op-11" in generate_world(_cfg(s, OPS11, days=1)).extra_board]
    kept = [s for s in range(40) if "op-11" not in generate_world(_cfg(s, OPS11, days=1)).extra_board]
    assert joined and kept


# ------------------------------------------------------------------ F13-R3: the dispatcher returns its own pulls
DAY = START


def at(d, h, m=0):
    return DAY + timedelta(days=d, hours=h, minutes=m)


STOP = {"spn": 110, "fmi": 0, "lamps": {"stop": True, "warn": True}}


def _rig(script):
    clock = SimClock(at(0, 4))
    mocks = Mocks.start(clock)
    mocks.seed(AG)
    eng = WorldEngine(GroundTruth(clock, script, seed=1), ProductLag(),
                      MockPorts(mocks, AG, build_fleet(AG, clock.now())), clock)
    return mocks, eng


def _sched(mocks, native):
    return [(c["status"], c["changed_at"], c["changed_by"])
            for c in mocks.clients["scheduling"].get("/status-changes", headers=H).json() if c["vehicle"] == native]


def test_two_stop_lamp_faults_a_day_apart_are_both_pulled_and_both_returned():
    """veh-3 = scheduling BUS-46 (in service at seed). Rows written from the ruling: pull at fault+12 min, back to
    in_service at return_to_service+12 min, and the second fault is pulled again (not skipped)."""
    script = DayScript(AG, (
        Beat(at(0, 9, 40), "veh-3", "fault_on", {**STOP, "withdrawn": True}),
        Beat(at(0, 10, 15), "veh-3", "fault_off", {"spn": 110, "fmi": 0}),
        Beat(at(0, 11, 30), "veh-3", "return_to_service", {}),
        Beat(at(1, 9, 0), "veh-3", "fault_on", {**STOP, "withdrawn": True}),
        Beat(at(1, 10, 0), "veh-3", "fault_off", {"spn": 110, "fmi": 0}),
        Beat(at(1, 11, 0), "veh-3", "return_to_service", {}),
    ))
    mocks, eng = _rig(script)
    eng.run_until(at(2, 0))
    assert _sched(mocks, "BUS-46") == [
        ("out_of_service", "2026-10-10T09:52:00Z", "dispatcher-sim"),
        ("in_service", "2026-10-10T11:42:00Z", "dispatcher-sim"),
        ("out_of_service", "2026-10-11T09:12:00Z", "dispatcher-sim"),
        ("in_service", "2026-10-11T11:12:00Z", "dispatcher-sim"),
    ]
    disp = [(o.kind, o.visible_at) for o in eng.observations_log if o.product == "scheduling"]
    assert disp == [("fault_on", at(0, 9, 52)), ("return_to_service", at(0, 11, 42)),
                    ("fault_on", at(1, 9, 12)), ("return_to_service", at(1, 11, 12))]
    assert not [s for s in eng.skipped if s.product == "scheduling"]


def test_the_dispatcher_does_not_return_a_bus_it_did_not_pull():
    """Scheduling shows out_of_service because someone else (the orchestrator, a person) pulled it: the return is
    theirs (A92: the orchestrator's approved return step). The dispatcher stands down with a reason."""
    script = DayScript(AG, (Beat(at(0, 11, 30), "veh-3", "return_to_service", {}),))
    mocks, eng = _rig(script)
    mocks.clients["scheduling"].post("/vehicles/BUS-46/unavailable", json={"changed_by": "system:run-1:pull:1"},
                                     headers=H)
    eng.run_until(at(0, 12))
    assert [s for _, _, s in [(0, 0, c[0]) for c in _sched(mocks, "BUS-46")]] == ["out_of_service"]
    (skip,) = [s for s in eng.skipped if s.product == "scheduling"]
    assert skip.kind == "return_to_service" and skip.at == at(0, 11, 42)


def test_the_dispatcher_does_not_undo_a_later_pull_by_someone_else():
    """It pulled the bus, then the orchestrator wrote its own out_of_service: the latest change is not this world's,
    so the dispatcher leaves the return to the orchestrator."""
    script = DayScript(AG, (Beat(at(0, 9, 40), "veh-3", "fault_on", {**STOP, "withdrawn": True}),
                            Beat(at(0, 11, 30), "veh-3", "return_to_service", {})))
    mocks, eng = _rig(script)
    eng.run_until(at(0, 10))
    mocks.clients["scheduling"].post("/vehicles/BUS-46/unavailable", json={"changed_by": "system:run-1:pull:1"},
                                     headers=H)
    eng.run_until(at(0, 12))
    assert [c[0] for c in _sched(mocks, "BUS-46")] == ["out_of_service", "out_of_service"]
    assert [s.kind for s in eng.skipped if s.product == "scheduling"] == ["return_to_service"]



def test_return_restores_the_status_the_bus_had_before_this_world_pulled_it():
    """A spare (veh-10 = BUS-53, ready at seed) pulled by the dispatcher goes back to ready, not to in_service (own
    decision, A156: the ruling's in_service is the status a bus in service had before the pull)."""
    script = DayScript(AG, (Beat(at(0, 9, 40), "veh-10", "fault_on", {**STOP, "withdrawn": True}),
                            Beat(at(0, 11, 30), "veh-10", "return_to_service", {})))
    mocks, eng = _rig(script)
    assert mocks.clients["scheduling"].get("/vehicles/BUS-53", headers=H).json()["status"] == "ready"
    eng.run_until(at(0, 12))
    assert [c[:2] for c in _sched(mocks, "BUS-53")] == [("out_of_service", "2026-10-10T09:52:00Z"),
                                                       ("ready", "2026-10-10T11:42:00Z")]


def test_recording_ports_follow_the_same_rule():
    ports = RecordingPorts()
    d = DispatcherActor(ProductLag())
    pull = Beat(at(0, 9, 40), "veh-3", "fault_on", {**STOP})
    back = Beat(at(0, 11, 30), "veh-3", "return_to_service", {})
    assert d.handles(back)
    assert d.act(pull, ports) is None and ports.scheduling_status("veh-3") == "out_of_service"
    assert d.act(back, ports) is None and ports.scheduling_status("veh-3") == "in_service"
    assert d.act(back, ports) is not None             # nothing of ours to return any more: stands down


# ------------------------------------------------------------------ F13-R3 over a 3-day seeded stochastic run
# A deliberately busy test fleet (p10 all-agency rate, 1,500 revenue miles a day: explicit test parameters, not realism) so that three days give a
# bus two stop-lamp episodes. The seed is FIXED (found once offline, never searched at test time).
SEEDED_MILES = 1500.0
SEED_3DAY = 77


def _stochastic_3day(seed):
    vehicles = tuple(f"veh-{n}" for n in range(1, 13))
    return generate_world(StochasticConfig(seed=seed, agency=AG, vehicles=vehicles, operators=(), start=START,
                                           days=3, revenue_miles_per_day=SEEDED_MILES, quantile="p10",
                                           fleet_cut="all"))


def _stop_episodes(w) -> dict[str, list]:
    """Per bus, the first stop-lamp fault_on of each fault episode (fault_id), in time order."""
    first: dict[tuple[str, str], datetime] = {}
    for b in w.script.beats:
        if b.kind == "fault_on" and (b.params.get("lamps") or {}).get("stop"):
            first.setdefault((b.vehicle, b.params["fault_id"]), b.at)
    out: dict[str, list] = {}
    for (veh, _), t in sorted(first.items(), key=lambda kv: kv[1]):
        out.setdefault(veh, []).append(t)
    return out


def test_three_day_seeded_run_a_later_stop_lamp_fault_on_the_same_bus_is_seen_again():
    w = _stochastic_3day(SEED_3DAY)
    repeat = {v: ts for v, ts in _stop_episodes(w).items() if len(ts) >= 2}
    assert repeat, "the fixed seed must give a bus two stop-lamp episodes in 3 days"
    clock = SimClock(START)
    mocks = Mocks.start(clock)
    mocks.seed(AG)
    eng = WorldEngine(GroundTruth(clock, w.script, seed=SEED_3DAY), ProductLag(),
                      MockPorts(mocks, AG, build_fleet(AG, clock.now())), clock)
    eng.run_until(START + timedelta(days=4))
    lag = ProductLag().delay("scheduling")
    for veh, starts in repeat.items():
        pulled = {o.truth_at for o in eng.observations_log
                  if o.product == "scheduling" and o.vehicle == veh and o.kind == "fault_on"}
        for t in starts[1:]:                       # the LATER episodes: seen (pulled) again, not skipped
            assert t in pulled, (veh, t, sorted(pulled))
            assert not [s for s in eng.skipped if s.product == "scheduling" and s.vehicle == veh
                        and s.kind == "fault_on" and s.truth_at == t]
        returned = [o for o in eng.observations_log
                    if o.product == "scheduling" and o.vehicle == veh and o.kind == "return_to_service"]
        assert len(returned) >= len(starts) - 1 and all(o.visible_at - o.truth_at == lag for o in returned)
