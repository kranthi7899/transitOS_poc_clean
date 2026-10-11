"""The Track C adapters call the clearance module (one rule implementation): graded fit, expiry, spare reuse,
capability profiles, against the REAL mock products."""
from __future__ import annotations

import shutil
from datetime import UTC, datetime

import pytest

from transitos.tools.idem_store import IdemStore
from transitos.tools.policy import CONFIG_DIR, KillSwitchStore
from transitos.tools.registry import Registry

from .conftest import AG, H
from .test_tools_vs_mocks import crosswalk

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")
D0, D1 = datetime(2026, 1, 5, 0, 0, tzinfo=UTC), datetime(2026, 1, 6, 0, 0, tzinfo=UTC)


def make_env(mocks, config_dir=None):
    mocks.ks = KillSwitchStore()
    mocks.reg = Registry(config_dir, clients={n: mocks.c(n) for n in ("maintenance", "scheduling", "workforce", "alerts")},
                         crosswalk=crosswalk(), idem=IdemStore(), killswitch=mocks.ks, sleep=lambda s: None)
    mocks.tools = mocks.reg.toolset(AG)
    return mocks


@pytest.fixture
def env(mocks):
    return make_env(mocks)


def test_larger_class_spare_is_allowed_by_default_and_refused_when_agency_says_exact(env, mocks, tmp_path):
    # BLK-5-2 is a cutaway block (veh-5); veh-10 is a standard bus: one class larger
    r = env.tools.reassign_block(AG, "BLK-5-2", "veh-10", "k1")
    assert r.status == "ok", r.note
    cfg = tmp_path / "cfg"
    shutil.copytree(CONFIG_DIR, cfg)
    with open(cfg / "policy" / f"{AG}.yaml", "a") as f:
        f.write("vehicle_fit: exact\n")
    from .conftest import Mocks
    strict = make_env(Mocks(), cfg)
    r = strict.tools.reassign_block(AG, "BLK-5-2", "veh-10", "k1")
    assert r.is_blocked and "larger class" in r.note and "vehicle_fit" in r.note


def test_smaller_class_spare_is_refused(env):
    r = env.tools.reassign_block(AG, "BLK-1-2", "veh-11", "k")      # cutaway for a standard block
    assert r.is_blocked and r.blocked_reason == "hard_limit"


def test_spare_serving_two_non_overlapping_blocks_is_allowed_but_not_two_overlapping(env):
    assert env.tools.reassign_block(AG, "BLK-1-2", "veh-10", "k1").status == "ok"      # 07-09
    assert env.tools.reassign_block(AG, "BLK-1-3", "veh-10", "k2").status == "ok"      # 09-11, same spare
    r = env.tools.reassign_block(AG, "BLK-2-2", "veh-10", "k3")                         # 07-09 again: overlap
    assert r.is_blocked and ("spare_not_ready" in r.note or "double_booking" in r.note)


def test_assign_operator_refused_on_expired_passenger_endorsement(env):
    env.c("workforce").post("/sim/operator", headers=H, json={
        "operator": "OP-319", "training_valid_to": "P=2026-01-05T00:00:00Z;standard=2030-01-01T00:00:00Z;lift=2030-01-01T00:00:00Z"})
    r = env.tools.assign_operator(AG, "BLK-1-2", "op-10", "k")        # block day is 2026-01-05: the day it expires
    assert r.is_blocked and "operator_training_expiry" in r.note or "expired" in (r.note or "")
    assert env.c("workforce").get("/assignments/BLK-1-2", headers=H).json()["operator"] == "OP-310"


def test_capability_profiles_report_what_each_product_supplies(env):
    r = env.tools.capability_profiles(AG)
    assert r.status == "ok"
    assert r.value["scheduling"]["fields"]["length_ft"] == "native"
    assert r.value["workforce"]["fields"]["licence_class"] == "native"
    env.c("workforce").post("/sim/operator-fields", headers=H, json={"drop": ["licence_class", "hours_week"]})
    r = env.tools.capability_profiles(AG)
    assert r.value["workforce"]["fields"]["licence_class"] == "unknown"
    assert r.value["workforce"]["fields"]["hours_week"] == "unknown"
    assert r.value["workforce"]["fields"]["hours_today"] == "native"


def test_list_blocks_is_the_schedule_feed_with_assignments_applied(env):
    r = env.tools.list_blocks(AG, D0, D1)
    assert r.status == "ok" and len(r.value) == 72          # 9 line vehicles x 8 blocks
    assert {b.id for b in r.value} >= {"BLK-1-1", "BLK-8-8"}


def test_a_malformed_operator_record_excludes_only_that_operator_in_the_read_adapter(env):
    """F4: a zone-less availability bound raises inside clearance; the adapter excludes THAT operator (fail
    closed per candidate) instead of failing the whole read."""
    block = env.tools.list_affected_blocks(AG, "veh-1", datetime(2026, 1, 5, 7, 0, tzinfo=UTC),
                                           datetime(2026, 1, 5, 9, 0, tzinfo=UTC)).value[0]
    env.c("workforce").post("/sim/operator", headers=H, json={"operator": "OP-311",
                                                              "available_from": "2026-01-05T05:00:00"})
    r = env.tools.list_eligible_operators(AG, block)
    assert r.status == "ok", r.note
    ids = {o.id for o in r.value}
    assert "op-2" not in ids and "op-1" in ids


def test_a_malformed_operator_record_blocks_the_write_with_a_clearance_error(env):
    env.c("workforce").post("/sim/operator", headers=H, json={"operator": "OP-319",
                                                              "available_from": "2026-01-05T05:00:00"})
    r = env.tools.assign_operator(AG, "BLK-1-2", "op-10", "k")
    assert r.is_blocked and r.blocked_reason == "hard_limit" and "clearance_error" in r.note, r.note
    assert env.c("workforce").get("/assignments/BLK-1-2", headers=H).json()["operator"] == "OP-310"


def test_eligible_operators_drop_known_violations_but_keep_unverifiable(env):
    block = env.tools.list_affected_blocks(AG, "veh-1", datetime(2026, 1, 5, 7, 0, tzinfo=UTC),
                                           datetime(2026, 1, 5, 9, 0, tzinfo=UTC)).value[0]
    env.c("workforce").post("/sim/operator", headers=H, json={
        "operator": "OP-311", "training_valid_to": "P=2026-01-05T00:00:00Z"})      # expired on the block day
    ids = {o.id for o in env.tools.list_eligible_operators(AG, block).value}
    assert "op-2" not in ids and "op-1" in ids
    env.c("workforce").post("/sim/operator-fields", headers=H,
                            json={"drop": ["qualifications", "licence_class", "endorsements", "trained_vehicle_types",
                                            "training_valid_to"]})
    ids = {o.id for o in env.tools.list_eligible_operators(AG, block).value}
    assert "op-2" in ids                       # now unknown, not violating: stays listed (flagged by the planner)
