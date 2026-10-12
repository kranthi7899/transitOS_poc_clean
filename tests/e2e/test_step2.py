"""Phase 10 Step 2 end to end: messy identifiers through the real integrated system (and the FakeWorld, which uses
the same rules resolver and the same fleet keys). Review Focus 2-5 at the scenario level; the rule-level rows are in
tests/plumbing/test_resolver.py and tests/plumbing/test_crosswalk_service_identity.py."""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from transitos.integration.world import IntegrationWorld
from transitos.verification.invariants import check_all
from transitos.verification.runner import classify, run_scenario
from transitos.verification.scenarios import BY_ID, Outcome
from transitos.verification.scenarios_step2 import NEW_ASSET, NEW_UNIT, fault


def test_ambiguous_match_writes_nothing_until_the_human_picks(make_world):
    """Review Focus 3: two or more candidates within 0.1 -> a human; zero runs and zero writes until the pick."""
    w = make_world()
    sc = BY_ID["ambiguous_match_human_pick"]
    w.apply(sc)
    w.deliver(sc.events[0])
    obs = w.observe()
    assert obs.runs == [] and obs.writes == [] and classify(obs) == Outcome.NEEDS_HUMAN_MATCH.value
    assert obs.dead_letters[0]["reason"].startswith("ambiguous")
    w.deliver(sc.events[0])                               # a redelivery changes nothing
    assert w.observe().runs == [] and w.observe().writes == []
    w.pick("veh-1")
    obs = w.observe()
    assert [r.vehicle_id for r in obs.runs] == ["veh-1"] and obs.runs[0].status == "AWAITING_APPROVAL"
    assert obs.writes == []                               # still nothing before approval
    w.approve(obs.runs[0].run_id)
    assert w.observe().writes


def test_lone_fuzzy_candidate_needs_a_human(make_world):
    """Review Focus 3: with one spare-less fleet, 'Bus 4' has a single candidate (Bus 44) below 0.9 -> a human."""
    sc = BY_ID["ambiguous_match"]
    one = replace(sc, id="lone_candidate", tweaks=replace(sc.tweaks, spares=0))
    res = run_scenario(make_world(), one, keep_observation=True)
    assert res.outcome == Outcome.NEEDS_HUMAN_MATCH.value and res.observation.writes == []


def test_dirty_ids_resolve_to_one_vehicle_and_one_run(make_world):
    """Review Focus 2: 't-077 '/'bus 044' and 'T 77'/'BUS-44 ' are veh-1: one run, the second event attached."""
    res = run_scenario(make_world(), BY_ID["dirty_id_normalised"], keep_observation=True)
    assert res.passed, res.mismatches
    obs = res.observation
    assert [r.vehicle_id for r in obs.runs] == ["veh-1"] and not obs.dead_letters
    assert check_all(obs) == []


def test_device_swap_old_unit_after_the_swap_is_parked(make_world):
    """Review Focus 4 end to end: the new unit's fault runs; the old unit's event dated after the swap is parked."""
    res = run_scenario(make_world(), BY_ID["device_swap"], keep_observation=True)
    assert res.passed, res.mismatches
    obs = res.observation
    assert len(obs.runs) == 1 and obs.runs[0].vehicle_id == "veh-1"
    assert [d["native"] for d in obs.dead_letters] == ["T-77"]
    assert sum(len(v) for v in obs.attached_events.values()) == 0


def test_device_swap_old_unit_before_the_swap_still_belongs_to_the_bus(make_world):
    sc = BY_ID["device_swap"]
    before = replace(sc, id="swap_before", events=(fault("T-77", -24 * 60 - 1),),
                     script=(("deliver", 0),))
    res = run_scenario(make_world(), before, keep_observation=True)
    assert [r.vehicle_id for r in res.observation.runs] == ["veh-1"] and not res.observation.dead_letters


def test_device_swap_exactly_at_the_swap_is_the_new_unit_only(make_world):
    sc = BY_ID["device_swap"]
    at_old = replace(sc, id="swap_at_old", events=(fault("T-77", -24 * 60),), script=(("deliver", 0),))
    res = run_scenario(make_world(), at_old, keep_observation=True)
    assert res.observation.runs == [] and [d["native"] for d in res.observation.dead_letters] == ["T-77"]
    at_new = replace(sc, id="swap_at_new", events=(fault(NEW_UNIT, -24 * 60),), script=(("deliver", 0),))
    res = run_scenario(make_world(), at_new, keep_observation=True)
    assert [r.vehicle_id for r in res.observation.runs] == ["veh-1"]


def test_retired_asset_blocks_the_work_order_and_nothing_else_is_written(make_world):
    """Review Focus 5 (work order blocked): the only write attempted is the refused work order."""
    res = run_scenario(make_world(), BY_ID["retired_asset_blocks_wo"], keep_observation=True)
    assert res.passed, res.mismatches
    obs = res.observation
    assert [w.action for w in obs.writes if w.ok] == []
    assert {w.action for w in obs.writes} <= {"create_work_order"}
    assert obs.runs[0].status == "ESCALATED" and obs.alerts == []


def test_orphan_unit_is_a_dead_letter(make_world):
    res = run_scenario(make_world(), BY_ID["orphan_telematics_unit"], keep_observation=True)
    assert res.passed and res.observation.dead_letters[0]["reason"].startswith("unresolved")


# ---------------------------------------------------------------- real system only
@pytest.fixture
def real():
    w = IntegrationWorld()
    yield w
    if w.system is not None:
        w.system.close()


def test_panel_unresolved_lists_candidates_and_evidence_and_the_pick_republishes(real):
    sc = BY_ID["ambiguous_match_human_pick"]
    real.apply(sc)
    real.deliver(sc.events[0])
    [item] = real.system.panel_client.get("/unresolved").json()
    assert item["outcome"] == "AMBIGUOUS" and item["native_id"] == "T-400"
    assert [c["canonical_id"] for c in item["candidates"]] == ["veh-1", "veh-2", "veh-3"]
    assert all(c["evidence"] and 0.6 <= c["score"] < 0.9 for c in item["candidates"])
    html = real.system.panel_client.get("/ui/unresolved").text
    assert "Pick veh-1" in html and "AMBIGUOUS" in html
    r = real.system.panel_client.post("/crosswalk", json={"agency": item["agency"], "product": "telematics",
                                                          "native_id": "T-400", "entity": "vehicle",
                                                          "canonical_id": "veh-2"})
    assert r.json()["republished"] == [sc.events[0].id]
    real.system.run_until_idle()
    assert [x.vehicle_id for x in real.observe().runs] == ["veh-2"]
    assert real.system.panel_client.get("/unresolved").json() == []


def test_renumbered_asset_work_order_goes_to_the_new_number(real):
    res = run_scenario(real, BY_ID["renumbered_asset"], keep_observation=True)
    assert res.passed, res.mismatches
    wos = real._docs("maintenance", "wo")
    assert [w["asset"] for w in wos] == [NEW_ASSET]


def test_retired_asset_refusal_comes_from_the_maintenance_product(real):
    """The product's 409 becomes a blocked (hard-limit) result in the adapter: attempted once, never retried."""
    res = run_scenario(real, BY_ID["retired_asset_blocks_wo"], keep_observation=True)
    assert [(w.action, w.ok) for w in res.observation.writes] == [("create_work_order", False)]
    [row] = real.system.store._q("SELECT detail FROM escalations")
    assert "hard_limit: asset_retired:1106" in row["detail"]


def test_f2_panel_pick_keeps_history_and_answers_409_instead_of_overwriting(real):
    """F2 through the real panel: the old unit's post-swap event parks; the human maps T-77 to veh-2. Before the
    swap T-77 is still veh-1; after the parked event it is veh-2. A second pick that would overwrite the dated
    history row (same key, set valid_to) is refused with 409 and the row in the way."""
    sc = BY_ID["device_swap"]
    real.apply(sc)
    late = sc.events[1]
    real.deliver(late)
    pc, xw = real.system.panel_client, real.system.xw
    r = pc.post("/crosswalk", json={"agency": "agency-a", "product": "telematics", "native_id": "T-77",
                                    "entity": "vehicle", "canonical_id": "veh-2"})
    assert r.status_code == 200 and r.json()["republished"] == [late.id]
    swap = sc.events[0].time - timedelta(minutes=1) - timedelta(hours=24)   # T0 - 24h is the swap
    assert xw.lookup("agency-a", "telematics", "T-77", at=swap - timedelta(minutes=1)) == "veh-1"
    assert xw.lookup("agency-a", "telematics", "T-77", at=late.time) == "veh-2"
    r = pc.post("/crosswalk", json={"agency": "agency-a", "product": "telematics", "native_id": "T-77",
                                    "entity": "vehicle", "canonical_id": "veh-3"})
    assert r.status_code == 409
    assert r.json()["detail"]["conflict"]["canonical_id"] in ("veh-1", "veh-2")
    assert xw.lookup("agency-a", "telematics", "T-77", at=swap - timedelta(minutes=1)) == "veh-1"


def test_g1_device_moved_through_the_real_panel(real):
    """G1 through the real panel: a stop-lamp fault from T-77 (veh-1's gateway) carries veh-2's VIN -> parked by
    the contradiction. The UI offers 'device moved' for it; a plain pick of veh-1 re-parks; POST /crosswalk with
    move=true to veh-2 republishes and the run starts for veh-2; an event dated before still resolves to veh-1;
    an identical second move changes nothing."""
    sc = BY_ID["dirty_id_normalised"]                        # identity tweaks: rows carry VINs
    real.apply(sc)
    from transitos.verification.scenarios_step2 import vehicle_identity
    vin2 = vehicle_identity(2, sc.tweaks)["sched_vin"]
    ev = fault("T-77", 5, native_vin=vin2)
    real.deliver(ev)
    pc, xw = real.system.panel_client, real.system.xw
    [item] = pc.get("/unresolved").json()
    assert item["contradiction"] is True and item["native_id"] == "T-77"
    html = pc.get("/ui/unresolved").text
    assert "Device moved to veh-2" in html
    body = {"agency": "agency-a", "product": "telematics", "native_id": "T-77", "entity": "vehicle"}
    r = pc.post("/crosswalk", json={**body, "canonical_id": "veh-1"})
    assert r.status_code == 200
    real.system.run_until_idle()
    assert real.observe().runs == [] and pc.get("/unresolved").json()          # re-parked
    r = pc.post("/crosswalk", params={"move": "true"}, json={**body, "canonical_id": "veh-2"})
    assert r.status_code == 200 and r.json()["republished"] == [ev.id]
    real.system.run_until_idle()
    assert [x.vehicle_id for x in real.observe().runs] == ["veh-2"]
    assert xw.lookup("agency-a", "telematics", "T-77", at=ev.time - timedelta(minutes=1)) == "veh-1"
    assert xw.lookup("agency-a", "telematics", "T-77", at=ev.time) == "veh-2"
    r = pc.post("/crosswalk", params={"move": "true"}, json={**body, "canonical_id": "veh-2"})
    assert r.status_code == 200 and r.json()["republished"] == []
    assert len([x for x in xw.rows("agency-a") if x.native_id == "T-77"]) == 2
