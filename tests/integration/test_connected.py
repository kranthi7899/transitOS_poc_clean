"""Offline connected profile: actual product APIs + actual graph, fake SQL transport only."""
from __future__ import annotations

import copy
import importlib.util
import re
from contextlib import ExitStack
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from transitos.integration.connected import AGENCY, MODULES, bootstrap, product_app
from transitos.integration.connected_world import ConnectedWorld, connected_console
from transitos.mocks.databricks_maintenance import Settings, WarehouseUnavailable
from transitos.tools.contract_tests import ALL_CHECKS, ContractCase
from transitos.verification.invariants import check_all
from transitos.verification.scenarios import BY_ID
from transitos.workflow.reasoning import StubReasoner

spec = importlib.util.spec_from_file_location("seed_export", Path(__file__).resolve().parents[2] / "scripts/export_databricks_seed.py")
seed_export = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seed_export)
collect = seed_export.collect

H = {"X-Agency": AGENCY}


class OfflineCloud:
    """Stateful revision/commit double, NOT a Databricks SQL syntax test."""
    def __init__(self):
        self.settings = Settings("example.cloud.databricks.com", "/sql/1.0/warehouses/test")
        data = collect()
        self.sources = {f"{p}.{t}": value["rows"] for p, tables in data.items() for t, value in tables.items()}
        self.rows, self.queries = {}, []
        self.available, self.fail_after_commit, self.conflict = True, False, False

    def close(self):
        pass

    def query(self, statement, parameters=()):
        if not self.available:
            raise WarehouseUnavailable
        self.queries.append((statement, parameters))
        if statement.startswith("CREATE"):
            return []
        if statement.startswith("SELECT *"):
            schema, table = re.search(r"`workspace`\.`(\w+)`\.`(\w+)`", statement).groups()
            return copy.deepcopy(self.sources[f"{schema}.{table}"])
        if statement.startswith("MERGE"):
            instance, agency, product, body, clock_at = parameters
            self.rows.setdefault((instance, agency, product), {
                "body": body, "clock_at": clock_at, "revision": 0, "commit_token": "bootstrap"})
            return []
        if statement.startswith("SELECT"):
            row = self.rows.get(tuple(parameters))
            return [copy.deepcopy(row)] if row else []
        if statement.startswith("UPDATE"):
            body, clock_at, token, instance, agency, product, revision = parameters
            row = self.rows[(instance, agency, product)]
            if row["revision"] == revision and not self.conflict:
                row.update(body=body, clock_at=clock_at, commit_token=token, revision=revision + 1)
            if self.fail_after_commit:
                self.fail_after_commit = False
                raise WarehouseUnavailable
            return []
        raise AssertionError(statement)


@pytest.fixture
def cloud():
    cloud = OfflineCloud()
    bootstrap(cloud, "test")
    return cloud


@pytest.fixture
def clients(cloud):
    with ExitStack() as stack:
        result = {p: stack.enter_context(TestClient(product_app(p, "test", cloud), headers=H)) for p in MODULES}
        yield result


def test_baseline_reads_are_cloud_scoped(cloud, clients):
    assert len(clients["scheduling"].get("/vehicles").json()) == 12
    assert len(clients["workforce"].get("/export/operators.csv").text.splitlines()) == 11
    assert len(clients["maintenance"].get("/assets").json()) == 12
    assert clients["maintenance"].get("/workorders").json() == []
    assert clients["telematics"].get("/vehicles", headers={"X-Agency": "wrong"}).status_code == 400
    for client in clients.values():
        assert client.post("/sim/reset").status_code == 409
        assert client.post("/sim/seed").status_code == 409
    cloud.available = False
    assert clients["maintenance"].get("/assets").status_code == 503


def test_all_effects_and_retry_marker_commit_together(cloud, clients):
    client = clients["maintenance"]
    body = {"asset": "1106", "description": "fault", "opened_by": "system"}
    headers = {"Idempotency-Key": "test:create:1"}
    cloud.fail_after_commit = True
    assert client.post("/workorders", headers=headers, json=body).status_code == 503
    # New API process/repository, same cloud row: replay returns original response.
    restarted = TestClient(product_app("maintenance", "test", cloud), headers=H)
    response = restarted.post("/workorders", headers=headers, json=body)
    assert response.status_code == 200 and response.json()["id"] == "wo-1"
    assert len(restarted.get("/workorders").json()) == 1
    assert restarted.post("/workorders", headers=headers, json={**body, "description": "other"}).status_code == 409
    cancelled = restarted.post("/workorders/wo-1/cancel", headers={"Idempotency-Key": "test:undo:1"})
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "closed"
    assert restarted.post("/workorders/wo-1/cancel", headers={"Idempotency-Key": "test:undo:1"}).json() == cancelled.json()
    assert restarted.post("/workorders", headers=headers, json=body).json()["status"] == "closed"


def test_revision_conflict_does_not_report_success(cloud, clients):
    cloud.conflict = True
    response = clients["maintenance"].post("/workorders", headers={"Idempotency-Key": "k"},
        json={"asset": "1106", "description": "test", "opened_by": "system"})
    assert response.status_code == 503
    cloud.conflict = False
    assert clients["maintenance"].get("/workorders").json() == []


def make_world(clients, tmp_path):
    world = ConnectedWorld(instance="test", clients=clients, data_dir=tmp_path / "orchestrator", reasoner=StubReasoner())
    world.apply(BY_ID["operator_swap_needed"])
    return world


def trigger(world):
    world.mocks.clients["telematics"].post("/sim/fault", json={
        "vehicle": "T-77", "spn": 520, "fmi": 3, "lamps": {"stop": True}}).raise_for_status()
    world.system.run_until_idle()
    return world.system.runs()[0]


def decision(world, run, scope="plan", version=None, acknowledged=None):
    return world.system.panel_client.post(f"/runs/{run['run_id']}/decision", json={
        "decision": "approve", "plan_version": version or run["plan_version"], "scope": scope,
        "user": "Synthetic acceptance script", "acknowledged": acknowledged or []})


def test_full_connected_workflow_uses_real_approvals_and_product_effects(cloud, clients, tmp_path):
    world = make_world(clients, tmp_path)
    run = trigger(world)
    assert run["status"] == "AWAITING_APPROVAL"
    assert world.system.writes.records == []
    assert clients["maintenance"].get("/workorders").json() == []
    assert decision(world, run, version=999).status_code == 409
    world.system.restart()  # interrupted graph recovers without authorizing a write
    assert world.system.writes.records == []
    assert decision(world, run).status_code == 200
    world.system.run_until_idle()
    run = world.system.runs()[0]
    assert run["status"] == "WAITING_FOR_REPAIR", run
    effects = [write.action for write in world.system.writes.records if write.ok and not write.compensation]
    assert set(effects) == {"create_work_order", "set_vehicle_unavailable", "reassign_block", "assign_operator", "publish_rider_alert"}
    assert effects[-1] == "publish_rider_alert"
    assert clients["scheduling"].get("/vehicles/BUS-44").json()["status"] == "out_of_service"
    assert clients["alerts"].get("/alerts").json() == []
    world.advance_clock(48)
    world.close_work_order(run["run_id"])
    run = world.system.runs()[0]
    assert run["status"] == "RETURNING"
    assert clients["scheduling"].get("/vehicles/BUS-44").json()["status"] == "out_of_service"
    assert decision(world, run, "return_to_service").status_code == 200
    world.system.run_until_idle()
    assert world.system.runs()[0]["status"] == "DONE"
    assert check_all(world.observe()) == []
    assert clients["alerts"].get("/alerts").json()
    world.system.close()


def test_cloud_console_preserves_profile_and_disallows_reset(clients, tmp_path):
    app = connected_console("test", reasoner=StubReasoner(), clients=clients, data_dir=tmp_path / "console")
    with TestClient(app) as client:
        state = client.get("/api/demo/state").json()
        assert state["agency"] == AGENCY and state["storage_profile"] == "databricks"
        assert len(state["vehicles"]) == 12
        assert state["product_records"]["workforce"]["assignment_path"] == "/assignments/BLK-1-1"
        response = client.post("/api/demo/command", headers={"X-TransitOS-Demo": "1"},
            json={"action": "reset", "generation": state["generation"]})
        assert response.status_code == 409


def test_kill_switch_blocks_approval_execution(clients, tmp_path):
    world = make_world(clients, tmp_path)
    run = trigger(world)
    world.set_kill_switch(True)
    # Even direct panel approval cannot override the tools' live kill-switch check.
    decision(world, run)
    world.system.run_until_idle()
    assert not [w for w in world.system.writes.records if w.ok]
    assert clients["maintenance"].get("/workorders").json() == []
    world.system.close()


def test_midplan_refusal_compensates_work_order_without_alert(clients, tmp_path):
    world = make_world(clients, tmp_path)
    run = trigger(world)
    clients["scheduling"].app.state.ctx.chaos.set("reject_write")
    assert decision(world, run).status_code == 200
    world.system.run_until_idle()
    assert world.system.runs()[0]["status"] == "ESCALATED"
    orders = clients["maintenance"].get("/workorders").json()
    assert len(orders) == 1 and orders[0]["status"] == "closed" and orders[0]["cancelled"]
    assert clients["alerts"].get("/sim/records/alerts").json() == []
    assert any(w.compensation and w.ok for w in world.system.writes.records)
    assert check_all(world.observe()) == []
    world.system.close()


def test_rejection_has_no_recovery_writes(clients, tmp_path):
    world = make_world(clients, tmp_path)
    run = trigger(world)
    response = world.system.panel_client.post(f"/runs/{run['run_id']}/decision", json={
        "decision": "reject", "plan_version": run["plan_version"], "user": "Synthetic rejection test", "scope": "plan"})
    assert response.status_code == 200
    world.system.run_until_idle()
    assert world.system.runs()[0]["status"] == "CLOSED_REJECTED"
    assert world.system.writes.records == []
    assert clients["maintenance"].get("/workorders").json() == []
    world.system.close()


@pytest.mark.parametrize("check", ALL_CHECKS, ids=lambda check: check.__name__)
def test_existing_tool_contracts_against_connected_products(clients, check):
    from datetime import timedelta

    from transitos.contracts.objects import Needs
    from transitos.mocks.clock import DEFAULT_START
    from transitos.mocks.crosswalk_seed import crosswalk_entries
    from transitos.tools.common import StaticCrosswalk
    from transitos.tools.policy import KillSwitchStore
    from transitos.tools.registry import Registry

    crosswalk = StaticCrosswalk()
    for entry in crosswalk_entries(AGENCY):
        crosswalk.resolve(entry)
    tools = Registry(clients=clients, crosswalk=crosswalk, killswitch=KillSwitchStore()).toolset(AGENCY)
    case = ContractCase(AGENCY, "veh-1", "veh-10", "BLK-1-1", (DEFAULT_START, DEFAULT_START + timedelta(hours=1)),
                        Needs(seats=20, wheelchair_positions=1, lift=True, type="standard"))
    check(tools, case)


def test_corrupt_cloud_snapshot_is_unavailable(cloud, clients):
    cloud.rows[("test", AGENCY, "maintenance")]["body"] = "not JSON"
    assert clients["maintenance"].get("/assets").status_code == 503


def test_connected_console_exports_full_fleet_and_assignment_evidence(clients, tmp_path):
    app = connected_console("test", reasoner=StubReasoner(), clients=clients, data_dir=tmp_path / "full-fleet")
    with TestClient(app) as client:
        s = client.get("/api/demo/state").json()
        assert len(s["vehicles"]) == 12 and len(s["blocks"]) == 72
        workforce = s["product_records"]["workforce"]["records"]
        assert len(workforce["/assignments"]) == 72
        assert len(workforce["/export/operators.csv"]["rows"]) == 10
        assert s["identities"]["op-10"]["workforce"] == "OP-319"
        assert s["storage_profile"] == "databricks" and s["writes"] == []
