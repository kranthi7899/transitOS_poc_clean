"""Phase 10 Step 4 scenarios through the REAL path (real mocks -> watch adapters -> System -> workflow -> tools ->
report). Expected times are written from the scripted days in scenarios_step4.py and the DST rules, not read back."""
from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

from transitos.contracts.envelope import TOPIC_RAW
from transitos.integration.world import make_world
from transitos.verification import report
from transitos.verification.fake_world import FakeWorld
from transitos.verification.runner import drive, run_scenario
from transitos.verification.scenarios import BY_ID, SCENARIOS, Outcome
from transitos.verification.scenarios_step4 import FALL_BACK, SPRING_FORWARD, WIRE_REASON, Step4Tweaks, _at, _coolant

IDS = ("repair_with_parts_wait", "human_wo_exists_pending", "local_time_normalised", "dst_fallback_event")
H = {"X-Agency": "agency-a"}


def z(*a) -> datetime:
    return datetime(*a, tzinfo=UTC)


def partial(sid: str, n_ops: int, **tweaks):
    """Run the first n_ops script ops of a scenario on the real system and return the world."""
    sc = BY_ID[sid]
    if tweaks:
        sc = dataclasses.replace(sc, tweaks=dataclasses.replace(sc.tweaks, **tweaks))
    w = make_world()
    drive(w, dataclasses.replace(sc, script=sc.script[:n_ops]))
    return w


def events(w, etype: str) -> list:
    return [e for _, _, e in w.system.bus.messages(TOPIC_RAW) if e.type == etype]


def maint(w, path: str):
    return w.mocks.clients["maintenance"].get(path, headers=H).json()


# ------------------------------------------------------------------ registration and the report
def test_registered_last_and_the_older_45_are_untouched():
    assert len(SCENARIOS) == 49 and [s.id for s in SCENARIOS[45:]] == list(IDS)
    assert all(not getattr(s.tweaks, "local_time", False) and getattr(s.tweaks, "maintenance_mode", "work_order")
               == "work_order" and getattr(s.tweaks, "clock_start", None) is None for s in SCENARIOS[:45])
    assert all(s.expect_work_orders is None and not s.real_world_reason for s in SCENARIOS[:45])


@pytest.mark.parametrize("sid", IDS)
def test_step4_scenarios_pass_on_the_real_system(sid):
    r = report.run_all(make_world, (BY_ID[sid],))[0]
    assert r.passed and not r.skipped, (r.mismatches, {k: v for k, v in r.invariants.items() if v})
    assert r.outcome == Outcome.PLAN_VALID.value


@pytest.mark.parametrize("sid", IDS)
def test_fakeworld_skips_them_with_the_reason_never_fakes_agreement(sid):
    r = run_scenario(FakeWorld(), BY_ID[sid])
    assert r.outcome == "SKIPPED" and r.skipped == WIRE_REASON


# ------------------------------------------------------------------ 4.1 pending repair
def test_parts_wait_keeps_the_run_in_wait_for_repair_until_the_close():
    # ops: world_until 09:41, approve, world_until 12:00
    w = partial("repair_with_parts_wait", 3)
    (run,) = w.observe().runs
    assert run.status == "WAITING_FOR_REPAIR"
    (pr,) = maint(w, "/pending-repairs")
    (wo,) = maint(w, "/workorders")
    assert pr["status"] == "CONVERTED" and pr["work_order"] == wo["id"] and run.wo_id == pr["id"]
    assert wo["status"] == "WAITING_PARTS" and wo["opened_at"] == "2026-10-10T09:51:00Z"   # 09:41 + 10 min
    assert events(w, "WorkOrderClosed") == []
    w.advance_world_to("13:30")                          # parts in at 12:51: IN_PROGRESS, still not a close
    assert maint(w, "/workorders")[0]["status"] == "IN_PROGRESS"
    assert w.observe().runs[0].status == "WAITING_FOR_REPAIR" and events(w, "WorkOrderClosed") == []
    w.advance_world_to("15:00")                          # return to service 14:00 -> shop closes 14:20
    (closed,) = events(w, "WorkOrderClosed")
    assert closed.time == z(2026, 10, 10, 14, 20)
    assert w.observe().runs[0].status != "WAITING_FOR_REPAIR"


def test_the_scenario_row_fails_if_the_run_leaves_wait_for_repair_early():
    """The report row itself (not only this file) catches a run that resumes during the parts wait."""
    sc = BY_ID["repair_with_parts_wait"]
    bad = dataclasses.replace(sc, script=(*sc.script[:3], ("expect_status", "RETURNING"), *sc.script[3:]))
    r = run_scenario(make_world(), bad)
    assert not r.passed and any("WAITING_FOR_REPAIR" in m for m in r.mismatches)


def test_a_person_opening_a_work_order_first_gets_the_request_no_duplicate():
    w = partial("human_wo_exists_pending", 3)            # to 10:30: shop opened at 10:00, foreman reached it 10:11
    (wo,) = maint(w, "/workorders")
    (pr,) = maint(w, "/pending-repairs")
    assert wo["opened_by"] == "human" and wo["opened_at"] == "2026-10-10T10:00:00Z"
    assert pr["status"] == "CONVERTED" and pr["work_order"] == wo["id"]
    assert [e.data["opened_by"] for e in events(w, "WorkOrderOpened")] == ["human"]


def test_pending_mode_with_a_human_work_order_before_the_trigger_creates_no_request():
    sc = BY_ID["human_wo_exists_pending"]
    sc = dataclasses.replace(sc, tweaks=dataclasses.replace(sc.tweaks, human_wo_exists=True), expect_wo_creates=0)
    r = run_scenario(make_world(), sc, keep_observation=True)
    assert r.passed, (r.mismatches, {k: v for k, v in r.invariants.items() if v})
    assert len(r.observation.work_orders) == 1 and r.observation.work_orders[0].source == "human"


# ------------------------------------------------------------------ 4.2 local time
def test_local_time_wire_is_really_local_and_the_bus_is_utc():
    w = partial("local_time_normalised", 3)             # to 12:00
    (wo,) = maint(w, "/workorders")
    assert wo["opened_at"] == "2026-10-10T05:41:00" and wo["utc_offset"]["opened_at"] == "-04:00"   # 09:41Z EDT
    (fault,) = [e for e in events(w, "VehicleFaultDetected")]
    assert fault.time == z(2026, 10, 10, 9, 40, 5)
    tel = w.mocks.apps["telematics"].state.ctx.store.all("agency-a", "events")
    assert tel and all(isinstance(e["payload"]["time"], int) for e in tel)
    assert [e.time for e in events(w, "WorkOrderOpened")] == [z(2026, 10, 10, 9, 41)]
    assert [e.time for e in events(w, "WorkOrderClosed")] == [z(2026, 10, 10, 11, 50)]
    for t in ("WorkOrderOpened", "WorkOrderClosed", "VehicleFaultDetected", "VehicleStatusChanged"):
        assert all(e.time.tzinfo is not None for e in events(w, t))


def test_local_time_gives_the_same_writes_as_the_utc_day():
    def actions(sid):
        obs = run_scenario(make_world(), BY_ID[sid], keep_observation=True).observation
        return [(x.action, x.ok, x.compensation) for x in obs.writes]
    assert actions("local_time_normalised") == actions("scripted_day_coolant")


def test_dst_fallback_close_with_an_earlier_local_time_is_stamped_06_30z():
    w = partial("dst_fallback_event", 5)                # to 07:00 (before the return approval)
    (wo,) = maint(w, "/workorders")
    assert (wo["opened_at"], wo["closed_at"]) == ("2026-11-01T01:41:00", "2026-11-01T01:30:00")
    assert wo["utc_offset"] == {"opened_at": "-04:00", "closed_at": "-05:00", "updated_at": "-05:00"}
    assert [e.time for e in events(w, "WorkOrderOpened")] == [z(2026, 11, 1, 5, 41)]
    assert [e.time for e in events(w, "WorkOrderClosed")] == [z(2026, 11, 1, 6, 30)]


def test_dst_fallback_without_offsets_follows_the_documented_rule_known_gap():
    """Known gap A112: with no utc_offset the second 01:30 is read as the FIRST (EDT): the close is stamped 05:30Z,
    BEFORE its own opening (05:41Z). The run still resumes (resume is by vehicle), so the outcome holds."""
    w = partial("dst_fallback_event", 5, utc_offset=False)
    assert [e.time for e in events(w, "WorkOrderClosed")] == [z(2026, 11, 1, 5, 30)]
    assert w.observe().runs[0].status != "WAITING_FOR_REPAIR"


def test_spring_forward_day_end_to_end():
    """2026-03-08: the clock jumps 02:00 EST -> 03:00 EDT. Opened 06:41Z (01:41 EST), closed 07:30Z (03:30 EDT)."""
    sc = BY_ID["dst_fallback_event"]
    sc = dataclasses.replace(sc, tweaks=dataclasses.replace(
        sc.tweaks, world_script=_coolant(SPRING_FORWARD, (6, 40), (7, 0), (7, 10)), clock_start=_at(SPRING_FORWARD, 5)),
        script=(("world_until", "06:41"), ("approve", "plan"), ("world_until", "08:00"),
                ("approve", "return_to_service")))
    r = run_scenario(make_world(), sc)
    assert r.passed, (r.mismatches, {k: v for k, v in r.invariants.items() if v})
    w = make_world()
    drive(w, dataclasses.replace(sc, script=sc.script[:3]))
    (wo,) = maint(w, "/workorders")
    assert (wo["opened_at"], wo["closed_at"]) == ("2026-03-08T01:41:00", "2026-03-08T03:30:00")
    assert [e.time for e in events(w, "WorkOrderClosed")] == [z(2026, 3, 8, 7, 30)]


def test_unknown_agency_timezone_escalates_degraded_never_guessed():
    """The products send zone-less local time but the agency has no timezone: the reads are unavailable and the run
    escalates without a write (unknown is never UTC)."""
    sc = BY_ID["local_time_normalised"]
    w = make_world()
    w.apply(sc)
    w.system.registry_settings["agency-a"]["timezone"] = None
    w.advance_world_to("09:41")
    obs = w.observe()
    assert [r.status for r in obs.runs] == ["ESCALATED"] and obs.writes == []
    assert dataclasses.is_dataclass(Step4Tweaks)
    assert FALL_BACK.tzinfo is not None
