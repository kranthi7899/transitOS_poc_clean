from __future__ import annotations

import pytest

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

from tools_fakes import AGENCY, T0, T1, World

from transitos.contracts.objects import Needs
from transitos.contracts.policy import Limits
from transitos.tools import policy as P
from transitos.tools.contract_tests import ALL_CHECKS, ContractCase
from transitos.tools.policy import KillSwitchStore
from transitos.tools.registry import Registry


class Env:
    def __init__(self) -> None:
        self.world = World()
        self.killswitch = KillSwitchStore()
        self.sleeps: list[float] = []
        self.registry = Registry(clients=self.world.clients(), crosswalk=self.world.crosswalk(),
                                 killswitch=self.killswitch, sleep=self.sleeps.append)
        self.tools = self.registry.toolset(AGENCY)
        self.case = ContractCase(agency=AGENCY, vehicle_id="veh-1", spare_id="veh-2", block_id="B-1",
                                 window=(T0, T1), needs=Needs(seats=25, wheelchair_positions=1, lift=True))


@pytest.fixture
def env() -> Env:
    return Env()


@pytest.mark.parametrize("check", ALL_CHECKS, ids=lambda f: f.__name__)
def test_contract_suite_v1_adapters(check, env):
    check(env.tools, env.case)


# ---- availability ------------------------------------------------------------------------------

def test_dead_product_is_unavailable_not_guess(env):
    env.world.down.add("scheduling")
    r = env.tools.get_vehicle(AGENCY, "veh-1")
    assert r.status == "unavailable" and r.product == "scheduling" and r.value is None
    assert len(env.sleeps) == 3  # retried with backoff before giving up
    assert env.sleeps == sorted(env.sleeps) and env.sleeps[1] > env.sleeps[0]


def test_unreachable_host_is_unavailable():
    import httpx
    reg = Registry(clients={"scheduling": httpx.Client(base_url="http://127.0.0.1:9", timeout=0.2)},
                   crosswalk=World().crosswalk(), killswitch=KillSwitchStore(), sleep=lambda s: None)
    r = reg.toolset(AGENCY).get_vehicle(AGENCY, "veh-1")
    assert r.status == "unavailable" and r.product == "scheduling"


def test_429_retried_then_ok(env):
    env.world.flaky["scheduling"] = 2
    assert env.tools.get_vehicle(AGENCY, "veh-1").status == "ok"
    assert len(env.sleeps) == 2


def test_history_partial_when_maintenance_down(env):
    env.world.down.add("maintenance")
    r = env.tools.get_vehicle_history(AGENCY, "veh-1")
    assert r.status == "partial" and r.product == "maintenance"


def test_agency_mismatch_rejected(env):
    with pytest.raises(ValueError):
        env.tools.get_vehicle("agency-999", "veh-1")


# ---- maintenance writes ------------------------------------------------------------------------

def test_create_work_order_retry_makes_one_wo(env):
    env.world.flaky["maintenance"] = 0
    a = env.tools.create_work_order(AGENCY, "veh-1", "fault", "run:veh-1:create_work_order:1")
    b = env.tools.create_work_order(AGENCY, "veh-1", "fault", "run:veh-1:create_work_order:1")
    assert a.value.id == b.value.id
    assert len(env.world.workorders) == 1


def test_idempotency_key_header_deduplicates_at_product(env):
    # bypass find_open (product-level proof): two raw POSTs with one key -> one WO
    c = env.world.clients()["maintenance"]
    h = {"Idempotency-Key": "k1", "X-Agency": AGENCY}
    body = {"asset": "1106", "description": "x", "opened_by": "system"}
    a, b = c.post("/workorders", json=body, headers=h).json(), c.post("/workorders", json=body, headers=h).json()
    assert a["id"] == b["id"]


def test_429_on_create_then_retry_single_wo(env):
    env.world.flaky["maintenance"] = 1  # first GET hits 429, retried
    r = env.tools.create_work_order(AGENCY, "veh-1", "fault", "k")
    assert r.status == "ok" and len(env.world.workorders) == 1


def test_human_opened_wo_is_reused(env):
    human = env.world.seed_wo("1106", opened_by="human")
    r = env.tools.create_work_order(AGENCY, "veh-1", "fault", "k")
    assert r.status == "ok" and r.note == "already_open" and r.value.id == human["id"]
    assert r.value.source == "human"
    assert env.world.count("maintenance", "POST", "/workorders") == 0


def test_cancel_only_system_created(env):
    human = env.world.seed_wo("1106", opened_by="human")
    r = env.tools.cancel_work_order(AGENCY, human["id"], "k")
    assert r.status == "unavailable" and r.product == "policy" and "hard_limit" in r.note
    assert env.world.workorders[human["id"]]["status"] == "open"
    assert env.world.count("maintenance", "POST", f"/workorders/{human['id']}/cancel") == 0


def test_create_with_unmapped_vehicle_is_unavailable(env):
    r = env.tools.create_work_order(AGENCY, "veh-99", "fault", "k")
    assert r.status == "unavailable"
    assert not env.world.workorders


# ---- idempotency store (scheduling / workforce / alerts) ---------------------------------------

def test_repeated_key_does_not_call_scheduling_again(env):
    env.tools.set_vehicle_unavailable(AGENCY, "veh-1", T0, None, "k-u")
    n = env.world.count("scheduling", "POST", "/vehicles/BUS-44/unavailable")
    env.world.vehicles["BUS-44"]["status"] = "ready"  # product state changed behind our back
    again = env.tools.set_vehicle_unavailable(AGENCY, "veh-1", T0, None, "k-u")
    assert again.status == "ok" and again.value.status == "out_of_service"  # prior result returned
    assert env.world.count("scheduling", "POST", "/vehicles/BUS-44/unavailable") == n == 1


def test_repeated_assign_operator_key_single_call(env):
    env.tools.assign_operator(AGENCY, "B-1", "op-311", "k-op")
    env.tools.assign_operator(AGENCY, "B-1", "op-311", "k-op")
    assert env.world.count("workforce", "POST", "/blocks/B-1/operator") == 1


def test_alert_repeated_key_single_alert(env):
    env.tools.publish_rider_alert(AGENCY, "B-1", "Delay", "k-a")
    env.tools.publish_rider_alert(AGENCY, "B-1", "Delay", "k-a")
    assert len(env.world.alerts) == 1


def test_failed_write_not_cached_so_retry_works(env):
    env.world.down.add("scheduling")
    assert env.tools.set_vehicle_unavailable(AGENCY, "veh-1", T0, None, "k").status == "unavailable"
    env.world.down.clear()
    assert env.tools.set_vehicle_unavailable(AGENCY, "veh-1", T0, None, "k").status == "ok"


# ---- kill switch -------------------------------------------------------------------------------

def _all_writes(t):
    return [
        t.create_work_order(AGENCY, "veh-1", "x", "k1"),
        t.cancel_work_order(AGENCY, "900", "k2"),
        t.set_vehicle_unavailable(AGENCY, "veh-1", T0, None, "k3"),
        t.set_vehicle_available(AGENCY, "veh-1", "k4"),
        t.reassign_block(AGENCY, "B-1", "veh-2", "k5"),
        t.assign_operator(AGENCY, "B-1", "op-311", "k6"),
        t.publish_rider_alert(AGENCY, "B-1", "m", "k7"),
    ]


def test_kill_switch_blocks_every_write_live(env):
    env.killswitch.set(AGENCY, True)
    results = _all_writes(env.tools)
    assert all(r.status == "unavailable" and r.product == "policy" and "kill_switch" in r.note for r in results)
    assert not [c for c in env.world.calls if c[1] == "POST"]
    # live: flip off without rebuilding anything
    env.killswitch.set(AGENCY, False)
    assert env.tools.create_work_order(AGENCY, "veh-1", "x", "k1").status == "ok"


def test_kill_switch_does_not_block_reads(env):
    env.killswitch.set(AGENCY, True)
    assert env.tools.get_vehicle(AGENCY, "veh-1").status == "ok"


def test_kill_switch_from_policy_file(tmp_path):
    (tmp_path / "policy").mkdir()
    (tmp_path / "policy" / "a.yaml").write_text("agency: a\nkill_switch: true\n")
    assert P.kill_switch("a", KillSwitchStore(), P.load_policy("a", tmp_path))
    assert not P.kill_switch("b", KillSwitchStore(), P.load_policy("b", tmp_path))


def test_load_policy_agency_042():
    p = P.load_policy(AGENCY)
    assert p.limits.min_spares == 1 and p.limits.max_operator_hours_day == 10.0


# ---- hard limits (adapters reject even if caller passes bad params) ----------------------------

def test_cannot_pull_last_spare(env):
    env.world.vehicles["BUS-46"]["status"] = "in_service"  # only BUS-45 remains ready
    r = env.tools.reassign_block(AGENCY, "B-1", "veh-2", "k")
    assert r.status == "unavailable" and "min_spares" in r.note
    assert env.world.blocks["B-1"]["vehicle"] == "BUS-44"
    assert env.world.count("scheduling", "POST", "/blocks/B-1/reassign") == 0


def test_spare_must_meet_needs(env):
    r = env.tools.reassign_block(AGENCY, "B-1", "veh-3", "k")  # BUS-46: 20 seats, no lift
    assert r.status == "unavailable" and "needs" in r.note
    assert env.world.blocks["B-1"]["vehicle"] == "BUS-44"


def test_reassign_to_non_ready_vehicle_rejected(env):
    env.world.vehicles["BUS-45"]["status"] = "out_of_service"
    r = env.tools.reassign_block(AGENCY, "B-1", "veh-2", "k")
    assert r.status == "unavailable" and "hard_limit" in r.note


def test_operator_hours_limit(env):
    r = env.tools.assign_operator(AGENCY, "B-1", "op-313", "k")  # 7.5h + 4h > 10
    assert r.status == "unavailable" and "operator_hours" in r.note
    assert env.world.count("workforce", "POST", "/blocks/B-1/operator") == 0


def test_lift_qualification_limit(env):
    r = env.tools.assign_operator(AGENCY, "B-1", "op-312", "k")
    assert r.status == "unavailable" and "lift_qualification" in r.note


def test_eligible_operators_filters_by_same_rules(env):
    block = env.tools.list_affected_blocks(AGENCY, "veh-1", T0, T1).value[0]
    ids = {o.id for o in env.tools.list_eligible_operators(AGENCY, block).value}
    # 310 is the current operator and eligible: listed, so the workflow can keep it (D's A13);
    # 312 not lift trained, 313 over hours
    assert ids == {"op-310", "op-311"}


def test_cannot_verify_limits_when_scheduling_flaky_blocks_feed(env):
    env.world.down.add("scheduling")
    r = env.tools.reassign_block(AGENCY, "B-1", "veh-2", "k")
    assert r.status == "unavailable" and r.product == "scheduling"


# ---- pure limit functions ----------------------------------------------------------------------

def test_pure_checks():
    lim = Limits()
    assert P.check_min_spares(1, lim) and not P.check_min_spares(2, lim)
    assert not P.check_min_spares(1, Limits(min_spares=0))
    assert not P.check_operator_hours(6.0, T0, T1, lim)       # exactly 10
    assert P.check_operator_hours(6.1, T0, T1, lim)
    assert P.check_hard_limits(limits=lim, ready_spares_before=1)


# ---- registry swap -----------------------------------------------------------------------------

def test_swap_product_is_config_line(tmp_path, env):
    import shutil
    cfg = tmp_path
    shutil.copytree(P.CONFIG_DIR, cfg, dirs_exist_ok=True)
    (cfg / "agency-077.yaml").write_text(
        (cfg / "agency-042.yaml").read_text().replace("agency-042", "agency-077")
        .replace("find_open_work_order: maintenance_mock_v1", "find_open_work_order: alt_maint"))
    calls = []

    class Alt:
        def find_open_work_order(self, agency, vehicle_id):
            calls.append(vehicle_id)
            from transitos.contracts.results import Result
            return Result.success(None)

    reg = Registry(cfg, clients=env.world.clients(), crosswalk=env.world.crosswalk("agency-077"),
                   killswitch=KillSwitchStore(), extra_adapters={"alt_maint": lambda deps: Alt()})
    ts = reg.toolset("agency-077")
    assert ts.adapter_ids["find_open_work_order"] == "alt_maint"
    assert ts.find_open_work_order("agency-077", "veh-1").value is None and calls == ["veh-1"]
    assert ts.get_vehicle("agency-077", "veh-1").status == "ok"  # other tools still v1


def test_unknown_adapter_id_fails_loudly(tmp_path):
    (tmp_path / "x.yaml").write_text("agency: x\ntools: {get_vehicle: nope}\n")
    with pytest.raises(KeyError):
        Registry(tmp_path).toolset("x")
