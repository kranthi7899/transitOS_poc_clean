"""Q4 (owner decision): the fall-back day with the block in the ambiguous hour ESCALATES, and the escalation hands a
person the reason (which 01:00 is meant) and the closest options, not just "nobody available" (A179-A184).

Expected rows and texts below are written from the spec (docs/assumptions/phase10-dst-escalation.md, the owner's Q4
text) before the implementation: the workforce CSV says "2026-11-01T01:00:00" for every operator's
available_from (the fleet stores 05:00Z); in America/New_York that wall time occurs at 05:00Z (EDT) and 06:00Z (EST);
the reader takes the later one (A145), so the 05:00Z-09:00Z block has no operator and the run escalates. Under the
earlier reading op-1 and op-2 would both be feasible."""
from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime

import pytest

from transitos.contracts.results import Result
from transitos.integration.world import make_world
from transitos.mocks.crosswalk_seed import crosswalk_entries
from transitos.tools.common import StaticCrosswalk, fold_readings
from transitos.tools.idem_store import IdemStore
from transitos.tools.policy import KillSwitchStore
from transitos.tools.registry import Registry, ToolSet
from transitos.verification import report
from transitos.verification.runner import run_scenario
from transitos.verification.scenarios import BY_ID, SCENARIOS, Outcome
from transitos.verification.scenarios_step4 import FALL_BACK, _at
from transitos.workflow import ambiguity
from transitos.workflow.ambiguity import question_text

from .conftest import AG, H

NY = "America/New_York"
QUESTION = "Which 01:00 is meant? 01:00 on 2026-11-01 (America/New_York) occurs twice: 05:00Z and 06:00Z"
NO_OPTION = "No candidate would be feasible for blk-1 under either reading"
EARLY, LATE ="2026-11-01T05:00:00Z", "2026-11-01T06:00:00Z"


# ------------------------------------------------------------------ the fold, as data (tools/common.py)
def test_fold_readings_gives_both_instants_earlier_first_only_in_the_repeated_hour():
    assert fold_readings("2026-11-01T01:00:00", NY) == (datetime(2026, 11, 1, 5, tzinfo=UTC),
                                                         datetime(2026, 11, 1, 6, tzinfo=UTC))
    assert fold_readings("2026-11-01T00:59:59", NY) is None          # before the repeated hour
    assert fold_readings("2026-11-01T02:00:00", NY) is None          # after it
    assert fold_readings("2026-03-08T02:30:00", NY) is None          # a spring-forward gap is not ambiguous


# ------------------------------------------------------------------ the question names the data's own time
def test_question_names_the_actual_local_time_and_date():
    assert question_text("2026-11-01T01:00:00", NY, [EARLY, LATE]) == QUESTION
    assert question_text("2026-10-25T02:30:00", "Europe/Berlin", ["2026-10-25T00:30:00Z", "2026-10-25T01:30:00Z"]) \
        == ("Which 02:30 is meant? 02:30 on 2026-10-25 (Europe/Berlin) occurs twice: 00:30Z and 01:30Z")
    # the UTC instants fall on another date than the local wall time: the date is named
    assert question_text("2026-04-05T02:30:00", "Australia/Sydney", ["2026-04-04T15:30:00Z", "2026-04-04T16:30:00Z"]) \
        == ("Which 02:30 is meant? 02:30 on 2026-04-05 (Australia/Sydney) occurs twice: 2026-04-04T15:30Z and "
            "2026-04-04T16:30Z")


# ------------------------------------------------------------------ the reader records each ambiguous cell
def toolset(mocks, stored: dict | None, *, local: bool = True) -> ToolSet:
    mocks.clock.advance(hours=(datetime(2026, 11, 1, 6, 30, tzinfo=UTC) - mocks.clock.now()).total_seconds() / 3600)
    wf = mocks.c("workforce")
    if stored:
        wf.post("/sim/operator", json={"operator": "OP-310", **stored}, headers=H).raise_for_status()
    if local:
        wf.post("/sim/time-format", json={"local_tz": NY}, headers=H)
    xw = StaticCrosswalk()
    for e in crosswalk_entries(AG):
        xw.add(AG, e.canonical_id, **{e.product: e.native_id})
    reg = Registry(clients={n: mocks.c(n) for n in ("maintenance", "scheduling", "workforce", "alerts")},
                   crosswalk=xw, idem=IdemStore(), killswitch=KillSwitchStore(), sleep=lambda s: None,
                   settings_patch={AG: {"local_time": local, "timezone": NY}})
    return reg.toolset(AG)


def test_an_ambiguous_csv_time_is_recorded_with_both_instants(mocks):
    ts = toolset(mocks, {"available_from": LATE})
    r = ts.time_ambiguities(AG)
    assert r.status == "ok"
    (a,) = [x for x in r.value if x["subject"] == "op-1"]
    assert a["product"] == "workforce" and a["field"] == "available_from" and a["timezone"] == NY
    assert a["local"] == "2026-11-01T01:00:00"
    assert a["readings"] == [EARLY, LATE]                      # earlier first
    assert a["read_as"] == LATE                                # the conservative reading the rules used (A145)
    assert a["record"]["id"] == "op-1" and a["record"]["available_from"].startswith("2026-11-01T06:00")


def test_no_ambiguity_outside_the_repeated_hour_or_with_utc_products(mocks):
    assert [x for x in toolset(mocks, {"available_from": "2026-11-01T08:00:00Z"}).time_ambiguities(AG).value
            if x["subject"] == "op-1"] == []
    assert toolset(mocks.__class__(), {"available_from": LATE}, local=False).time_ambiguities(AG).value == []


# ------------------------------------------------------------------ the scenarios
def test_dst_fallback_event_is_restored_to_its_original_clock_and_escalates():
    sc = BY_ID["dst_fallback_event"]
    assert sc.tweaks.clock_start == _at(FALL_BACK, 4)          # 04:00Z: the block is 05:00Z-09:00Z
    assert sc.expected == Outcome.ESCALATE_NO_SAFE_OPTION
    assert QUESTION in sc.expect_escalation_contains


def test_the_moved_later_day_is_its_own_success_scenario():
    sc = BY_ID["dst_fallback_after_ambiguous_hour"]
    assert sc.tweaks.clock_start == _at(FALL_BACK, 5) and sc.expected == Outcome.PLAN_VALID
    assert sc.expect_event_times == (("WorkOrderOpened", "2026-11-01T05:41:00+00:00"),
                                     ("WorkOrderClosed", "2026-11-01T06:30:00+00:00"))
    assert sc.expect_escalation_contains is None
    assert len(SCENARIOS) == 50 and SCENARIOS[-1].id == "dst_fallback_after_ambiguous_hour"
    assert all(s.expect_escalation_contains is None for s in SCENARIOS if s.id != "dst_fallback_event")


@pytest.fixture(scope="module")
def escalated():
    w = make_world()
    r = run_scenario(w, BY_ID["dst_fallback_event"], keep_observation=True)
    return w, r


def test_the_report_row_passes_on_the_real_system(escalated):
    _, r = escalated
    assert r.outcome == Outcome.ESCALATE_NO_SAFE_OPTION.value and r.passed, (r.mismatches, r.invariants)
    assert r.observation.writes == []
    assert r.observation.runs[0].violations == ["no_feasible_operator:blk-1"]


def test_the_escalation_carries_the_question_and_the_closest_options(escalated):
    _, r = escalated
    (run,) = r.observation.runs
    detail = r.observation.escalations[run.run_id]
    (nh,) = detail["needs_human"]
    assert nh["kind"] == "ambiguous_local_time" and nh["question"] == QUESTION
    assert nh["field"] == "available_from" and nh["block"] == "blk-1"
    assert nh["subjects"] == [{"product": "workforce", "id": "op-1"}, {"product": "workforce", "id": "op-2"}]
    assert nh["readings"] == [EARLY, LATE] and nh["read_as"] == LATE
    ops = nh["options"]
    assert [(o["operator"], o["reading"]) for o in ops] == [("op-1", EARLY), ("op-2", EARLY)]
    for o in ops:   # the option is a whole re-plan of the block, judged by clearance: a spare and what is left
        assert o["spare"] in ("veh-2", "veh-3") and isinstance(o["still_to_acknowledge"], list)
        assert o["text"].startswith(f"{o['operator']} if 01:00 means 05:00Z")
    assert all(o["reading"] != LATE for o in ops)          # the conservative reading is what excluded them


def test_the_decision_record_and_the_panel_show_it(escalated):
    w, r = escalated
    (run,) = r.observation.runs
    (rec,) = w.system.decisions.list_for_run(run.run_id)
    assert rec.outcome == "escalated"
    assert rec.recommendation["needs_human"][0]["question"] == QUESTION
    html = w.system.panel_client.get(f"/ui/runs/{run.run_id}").text
    assert "Needs a person" in html and QUESTION in html
    assert "op-1 if 01:00 means 05:00Z" in html and "op-2 if 01:00 means 05:00Z" in html


def test_the_report_row_fails_when_the_payload_lacks_the_expected_text():
    sc = dataclasses.replace(BY_ID["dst_fallback_event"], expect_escalation_contains=("Which 03:00 is meant?",))
    r = run_scenario(make_world(), sc)
    assert not r.passed and any("Which 03:00 is meant?" in m for m in r.mismatches)


def test_the_report_json_has_the_row(escalated):
    row = report.to_json([escalated[1]])["scenarios"][0]
    assert row["id"] == "dst_fallback_event" and row["passed"] and row["outcome"] == "escalate-no-safe-option"


def test_when_no_reading_makes_anyone_feasible_the_escalation_says_so():
    """The same day with operators that are not qualified for the vehicle: the time is still ambiguous, but neither
    reading would make anyone feasible, and the record says that in words instead of offering nothing silently."""
    sc = BY_ID["dst_fallback_event"]
    sc = dataclasses.replace(sc, tweaks=dataclasses.replace(sc.tweaks, operators_qualified=False),
                             expect_escalation_contains=(QUESTION, NO_OPTION))
    r = run_scenario(make_world(), sc, keep_observation=True)
    assert r.passed, r.mismatches
    (nh,) = r.observation.escalations[r.observation.runs[0].run_id]["needs_human"]
    assert nh["options"] == []


# ------------------------------------------------------------------ no information, or a failure: as today
def test_without_ambiguity_info_the_escalation_is_exactly_as_before(monkeypatch):
    monkeypatch.setattr(ToolSet, "time_ambiguities", lambda self, agency: Result.success(None, note="not_supported"))
    r = run_scenario(make_world(), dataclasses.replace(BY_ID["dst_fallback_event"], expect_escalation_contains=None),
                     keep_observation=True)
    assert r.passed, r.mismatches
    detail = r.observation.escalations[r.observation.runs[0].run_id]
    assert "needs_human" not in detail and "needs_human_error" not in detail
    assert r.observation.runs[0].violations == ["no_feasible_operator:blk-1"]


def test_a_failure_building_the_question_still_escalates_and_says_so(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bad payload")
    monkeypatch.setattr(ambiguity, "needs_human_for", boom)
    r = run_scenario(make_world(), dataclasses.replace(BY_ID["dst_fallback_event"], expect_escalation_contains=None),
                     keep_observation=True)
    assert r.outcome == Outcome.ESCALATE_NO_SAFE_OPTION.value and r.observation.writes == []
    detail = r.observation.escalations[r.observation.runs[0].run_id]
    assert "needs_human" not in detail
    assert "could not be worked out" in detail["needs_human_error"] and "bad payload" in detail["needs_human_error"]


def test_the_payload_is_plain_json():
    # what the store, the decision record and the PG profile persist must round-trip as JSON
    w = make_world()
    r = run_scenario(w, BY_ID["dst_fallback_event"], keep_observation=True)
    detail = r.observation.escalations[r.observation.runs[0].run_id]
    assert json.loads(json.dumps(detail)) == detail
