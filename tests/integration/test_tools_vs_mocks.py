"""Track C tool adapters against Track A's REAL mock products (not C's fakes). Same contract suite."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from transitos.contracts.objects import Needs
from transitos.mocks.crosswalk_seed import crosswalk_entries
from transitos.tools.common import StaticCrosswalk
from transitos.tools.contract_tests import ALL_CHECKS, ContractCase
from transitos.tools.idem_store import IdemStore
from transitos.tools.policy import KillSwitchStore
from transitos.tools.registry import Registry

from .conftest import AG, H

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

W0, W1 = datetime(2026, 1, 5, 7, 0, tzinfo=UTC), datetime(2026, 1, 5, 9, 0, tzinfo=UTC)  # BLK-1-2


def crosswalk() -> StaticCrosswalk:
    xw = StaticCrosswalk()
    for e in crosswalk_entries(AG):
        xw.add(AG, e.canonical_id, **{e.product: e.native_id})
    return xw


@pytest.fixture
def env(mocks):
    mocks.ks = KillSwitchStore()
    mocks.reg = Registry(clients={n: mocks.c(n) for n in ("maintenance", "scheduling", "workforce", "alerts")},
                         crosswalk=crosswalk(), idem=IdemStore(), killswitch=mocks.ks, sleep=lambda s: None)
    mocks.tools = mocks.reg.toolset(AG)
    return mocks


CASE = ContractCase(agency=AG, vehicle_id="veh-1", spare_id="veh-10", block_id="BLK-1-2", window=(W0, W1),
                    needs=Needs(seats=20, wheelchair_positions=1, lift=True, type="standard"))


@pytest.mark.parametrize("check", ALL_CHECKS, ids=lambda f: f.__name__)
def test_contract_suite_against_real_mocks(check, env):
    check(env.tools, CASE)


def test_eligible_operators_parse_real_csv_and_include_current(env):
    block = env.tools.list_affected_blocks(AG, "veh-1", W0, W1).value[0]
    r = env.tools.list_eligible_operators(AG, block)
    assert r.status == "ok", r.note
    ids = {o.id for o in r.value}
    assert block.operator_id == "op-1" and "op-1" in ids          # current operator, if eligible, is listed
    assert "op-3" not in ids                                        # 9.5h + 2h > 10h
    op1 = next(o for o in r.value if o.id == "op-1")
    assert op1.available_from == datetime(2026, 1, 5, 5, 0, tzinfo=UTC)


def test_assign_operator_reads_back_from_workforce(env):
    r = env.tools.assign_operator(AG, "BLK-1-2", "op-10", "k-op")
    assert r.status == "ok", r.note
    assert r.value.operator_id == "op-10"
    assert env.c("workforce").get("/assignments/BLK-1-2", headers=H).json()["operator"] == "OP-319"


def test_our_status_writes_are_marked_as_ours(env):
    env.tools.set_vehicle_unavailable(AG, "veh-1", W0, None, "run-x:set_vehicle_unavailable:1")
    (ch,) = env.c("scheduling").get("/status-changes", headers=H).json()
    assert ch["changed_by"] == "transitos:run-x:set_vehicle_unavailable:1"


def test_create_work_order_retry_single_wo_at_product(env):
    a = env.tools.create_work_order(AG, "veh-1", "fault", "k1")
    b = env.tools.create_work_order(AG, "veh-1", "fault", "k1")
    assert a.status == b.status == "ok" and a.value.id == b.value.id and a.value.source == "system"
    assert len(env.c("maintenance").get("/workorders", headers=H).json()) == 1


def test_human_work_order_reused_and_never_cancelled(env):
    h = env.c("maintenance").post("/workorders", json={"asset": "1106", "description": "brake",
                                                       "opened_by": "human"}, headers=H).json()
    r = env.tools.create_work_order(AG, "veh-1", "fault", "k1")
    assert r.status == "ok" and r.note == "already_open" and r.value.id == h["id"] and r.value.source == "human"
    c = env.tools.cancel_work_order(AG, h["id"], "k2")
    assert c.is_blocked and c.blocked_reason == "hard_limit"


def test_maintenance_429_is_unavailable_and_creates_nothing(env):
    env.chaos["maintenance"].set("http_429")
    r = env.tools.create_work_order(AG, "veh-1", "fault", "k1")
    assert r.status == "unavailable" and r.product == "maintenance" and not r.is_blocked
    env.chaos["maintenance"].clear()
    assert env.c("maintenance").get("/workorders", headers=H).json() == []


def test_scheduling_timeout_read_is_unavailable_never_guessed(env):
    env.chaos["scheduling"].set("timeout")
    r = env.tools.get_vehicle(AG, "veh-1")
    assert r.status == "unavailable" and r.product == "scheduling" and r.value is None


def test_reject_write_is_product_failure_not_policy(env):
    env.chaos["scheduling"].set("reject_write")
    r = env.tools.reassign_block(AG, "BLK-1-2", "veh-10", "k")
    assert r.status == "unavailable" and r.product == "scheduling" and not r.is_blocked


def test_kill_switch_blocks_before_product(env):
    env.ks.set(AG, True)
    r = env.tools.set_vehicle_unavailable(AG, "veh-1", W0, None, "k")
    assert r.is_blocked and r.blocked_reason == "kill_switch"
    assert env.c("scheduling").get("/status-changes", headers=H).json() == []


def test_compensating_reassign_restores_original_vehicle(env):
    assert env.tools.reassign_block(AG, "BLK-1-2", "veh-10", "k").status == "ok"
    back = env.tools.reassign_block(AG, "BLK-1-2", "veh-1", "k:comp", compensation=True)
    assert back.status == "ok" and back.value.vehicle_id == "veh-1"


def test_registry_base_urls_match_mock_runner_ports():
    import yaml

    from transitos.mocks.run import PORTS
    from transitos.tools.policy import CONFIG_DIR
    cfg = yaml.safe_load((CONFIG_DIR / f"{AG}.yaml").read_text())
    for product, p in cfg["products"].items():
        assert p["base_url"] == f"http://localhost:{PORTS[product]}", product
