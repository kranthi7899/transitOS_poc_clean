"""Step 4 fix round F2: the workforce CSV carries no utc_offset, so a time in the repeated fall-back hour (01:00-01:59
on 2026-11-01, America/New_York) is read with the CONSERVATIVE occurrence per field (ruling, A145):
  later  (fold=1): available_from, last_duty_end            (later start of availability, shorter rest)
  earlier(fold=0): available_to, duty_start_today, medical_valid_to, training_valid_to (every expiry)
Rows written from the ruling text before the change."""
from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

from transitos.integration.world import make_world
from transitos.mocks.crosswalk_seed import crosswalk_entries
from transitos.tools.common import StaticCrosswalk
from transitos.tools.idem_store import IdemStore
from transitos.tools.policy import KillSwitchStore
from transitos.tools.registry import Registry
from transitos.verification.runner import run_scenario
from transitos.verification.scenarios import BY_ID
from transitos.verification.scenarios_step4 import FALL_BACK, _at

from .conftest import AG, H

NY = "America/New_York"
FIRST, SECOND = datetime(2026, 11, 1, 5, 30, tzinfo=UTC), datetime(2026, 11, 1, 6, 30, tzinfo=UTC)  # both "01:30"


def operators(mocks, stored: dict) -> dict:
    mocks.clock.advance(hours=(SECOND - mocks.clock.now()).total_seconds() / 3600)
    wf = mocks.c("workforce")
    wf.post("/sim/operator", json={"operator": "OP-310", **stored}, headers=H).raise_for_status()
    wf.post("/sim/time-format", json={"local_tz": NY}, headers=H)
    xw = StaticCrosswalk()
    for e in crosswalk_entries(AG):
        xw.add(AG, e.canonical_id, **{e.product: e.native_id})
    reg = Registry(clients={n: mocks.c(n) for n in ("maintenance", "scheduling", "workforce", "alerts")},
                   crosswalk=xw, idem=IdemStore(), killswitch=KillSwitchStore(), sleep=lambda s: None,
                   settings_patch={AG: {"local_time": True, "timezone": NY}})
    ts = reg.toolset(AG)
    return {o.id: o for o in ts._optional("operators")[0](AG)}


def z(s: str) -> str:
    return s.replace("+00:00", "Z")


@pytest.mark.parametrize(("field", "stored", "want"), [
    ("available_from", SECOND, SECOND),      # reviewer repro: stored 06:30Z renders 01:30 -> must read 06:30Z
    ("available_from", FIRST, SECOND),       # the conservative reading of an ambiguous start: the later one
    ("last_duty_end", FIRST, SECOND),
    ("available_to", SECOND, FIRST),         # the conservative end: the earlier one
    ("duty_start_today", SECOND, FIRST),
    ("medical_valid_to", SECOND, FIRST),     # every expiry: the earlier one
])
def test_conservative_occurrence_per_field_in_the_repeated_hour(mocks, field, stored, want):
    op = operators(mocks, {field: z(stored.isoformat())})["op-1"]
    assert getattr(op, field) == want


def test_training_expiry_reads_the_earlier_occurrence(mocks):
    op = operators(mocks, {"training_valid_to": f"P={z(SECOND.isoformat())}"})["op-1"]
    assert op.training_valid_to["P"] == FIRST


def test_the_csv_really_says_01_30(mocks):
    operators(mocks, {"available_from": z(SECOND.isoformat())})
    text = mocks.c("workforce").get("/export/operators.csv", headers=H).text
    assert "2026-11-01T01:30:00" in text


def test_outside_the_repeated_hour_nothing_changes(mocks):
    t = datetime(2026, 11, 1, 8, 0, tzinfo=UTC)                         # 03:00 EST, not ambiguous
    op = operators(mocks, {"available_from": z(t.isoformat()), "available_to": z(t.isoformat())})["op-1"]
    assert op.available_from == op.available_to == t


def test_through_clearance_on_the_fall_back_day_an_ambiguous_start_is_not_available():
    """dst_fallback_event's day with the block at 05:00Z (clock_start 04:00Z). Every operator's availability starts
    05:00Z, which the CSV says as "01:00" (ambiguous). Read conservatively it is 06:00Z, AFTER the block starts: no
    operator is known to be available, so the run escalates with zero writes. (The permissive reading, 05:00Z, would
    have staffed the block.)"""
    sc = BY_ID["dst_fallback_event"]
    sc = dataclasses.replace(sc, tweaks=dataclasses.replace(sc.tweaks, clock_start=_at(FALL_BACK, 4)),
                             expected=sc.expected.__class__("escalate-no-safe-option"), expect_wo_creates=0,
                             expect_work_orders=None, expect_event_times=None, expect_pending_repairs=None,
                             script=(("world_until", "05:41"),), known_gap="")
    r = run_scenario(make_world(), sc, keep_observation=True)
    assert r.outcome == "escalate-no-safe-option", (r.outcome, r.mismatches)
    assert r.observation.writes == []
    assert r.observation.runs[0].violations == ["no_feasible_operator:blk-1"]
    # the same day with every product on UTC (knob off) staffs the block: the escalation is the conservative fold
    utc = dataclasses.replace(sc, tweaks=dataclasses.replace(sc.tweaks, local_time=False))
    assert run_scenario(make_world(), utc).outcome.startswith("in-progress")   # proposed, awaiting approval


def test_dst_fallback_after_ambiguous_hour_block_starts_after_the_ambiguous_hour():
    """The moved-later day (block 06:00Z-10:00Z) is staffed: the conservative 06:00Z start is not after it. Owner
    decision Q4 (A183): dst_fallback_event itself keeps its original 04:00Z clock and escalates."""
    assert BY_ID["dst_fallback_after_ambiguous_hour"].tweaks.clock_start == _at(FALL_BACK, 5)
    assert BY_ID["dst_fallback_event"].tweaks.clock_start == _at(FALL_BACK, 4)
