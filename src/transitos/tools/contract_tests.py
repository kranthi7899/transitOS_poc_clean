"""Reusable contract suite for any ReadTools/WriteTools implementation (adapter set, fake, v2 product).

Usage (pytest):
    @pytest.mark.parametrize("check", ALL_CHECKS)
    def test_contract(check, my_tools, my_case): check(my_tools, my_case)

Each check gets a fresh, seeded tool set (the caller's fixture is function-scoped) and a ContractCase
describing the seed: a vehicle with one block and a work-order-free asset, a qualifying spare, and
an eligible operator. Checks assert only behaviour promised by contracts/tools.py."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from transitos.contracts.objects import Needs


@dataclass
class ContractCase:
    agency: str
    vehicle_id: str            # canonical; has block_id assigned, no open work order
    spare_id: str              # canonical; ready, meets block needs
    block_id: str
    window: tuple[datetime, datetime]
    needs: Needs
    unknown_vehicle_id: str = "veh-does-not-exist"


def check_get_vehicle(tools, c: ContractCase) -> None:
    r = tools.get_vehicle(c.agency, c.vehicle_id)
    assert r.status == "ok" and r.value.id == c.vehicle_id and r.value.agency == c.agency


def check_unknown_vehicle_never_guessed(tools, c: ContractCase) -> None:
    r = tools.get_vehicle(c.agency, c.unknown_vehicle_id)
    assert r.status == "unavailable" and r.value is None


def check_affected_blocks(tools, c: ContractCase) -> None:
    r = tools.list_affected_blocks(c.agency, c.vehicle_id, *c.window)
    assert r.status == "ok" and c.block_id in {b.id for b in r.value}
    assert all(b.vehicle_id == c.vehicle_id for b in r.value)


def check_ready_vehicles_meet_needs(tools, c: ContractCase) -> None:
    r = tools.list_ready_vehicles(c.agency, *c.window, c.needs)
    assert r.status == "ok" and c.spare_id in {v.id for v in r.value}
    assert all(v.status == "ready" and v.seats >= c.needs.seats for v in r.value)
    assert c.vehicle_id not in {v.id for v in r.value}


def check_eligible_operators(tools, c: ContractCase) -> None:
    block = tools.list_affected_blocks(c.agency, c.vehicle_id, *c.window).value[0]
    r = tools.list_eligible_operators(c.agency, block)
    assert r.status == "ok" and r.value
    assert all(o.lift_trained or not block.needs.lift for o in r.value)


def check_no_open_wo_then_create_is_idempotent(tools, c: ContractCase) -> None:
    assert tools.find_open_work_order(c.agency, c.vehicle_id).value is None
    k = f"{c.agency}:{c.vehicle_id}:contract:create_work_order:1"
    a = tools.create_work_order(c.agency, c.vehicle_id, "fault", k)
    b = tools.create_work_order(c.agency, c.vehicle_id, "fault", k)
    assert a.status == "ok" and b.status == "ok" and a.value.id == b.value.id
    assert tools.find_open_work_order(c.agency, c.vehicle_id).value.id == a.value.id
    assert tools.get_work_order(c.agency, a.value.id).value.status == "open"


def check_work_order_compensation_pair(tools, c: ContractCase) -> None:
    wo = tools.create_work_order(c.agency, c.vehicle_id, "fault", "k-wo-comp").value
    assert wo.source == "system"
    done = tools.cancel_work_order(c.agency, wo.id, "k-wo-comp:comp")
    assert done.status == "ok" and done.value.status == "closed"
    assert tools.find_open_work_order(c.agency, c.vehicle_id).value is None


def check_unavailable_available_pair(tools, c: ContractCase) -> None:
    r = tools.set_vehicle_unavailable(c.agency, c.vehicle_id, c.window[0], c.window[1], "k-unavail")
    assert r.status == "ok" and r.value.status == "out_of_service"
    r = tools.set_vehicle_available(c.agency, c.vehicle_id, "k-avail")
    assert r.status == "ok" and r.value.status == "ready"


def check_reassign_and_back(tools, c: ContractCase) -> None:
    r = tools.reassign_block(c.agency, c.block_id, c.spare_id, "k-re")
    assert r.status == "ok" and r.value.vehicle_id == c.spare_id
    back = tools.reassign_block(c.agency, c.block_id, c.vehicle_id, "k-re:comp", compensation=True)
    assert back.status == "ok" and back.value.vehicle_id == c.vehicle_id


def check_rider_alert(tools, c: ContractCase) -> None:
    r = tools.publish_rider_alert(c.agency, c.block_id, "Route delayed", "k-alert")
    r2 = tools.publish_rider_alert(c.agency, c.block_id, "Route delayed", "k-alert")
    assert r.status == "ok" and r.value == r2.value  # repeated key -> same alert, not a second one


ALL_CHECKS = [
    check_get_vehicle, check_unknown_vehicle_never_guessed, check_affected_blocks,
    check_ready_vehicles_meet_needs, check_eligible_operators, check_no_open_wo_then_create_is_idempotent,
    check_work_order_compensation_pair, check_unavailable_available_pair, check_reassign_and_back,
    check_rider_alert,
]
