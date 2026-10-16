"""Offline API/storage-boundary tests; never contact Databricks or OpenAI.

SQLite executes reads/updates and the MERGE source SELECT. Its INSERT emulates
the insert-only MERGE effect; live Databricks syntax/auth remain a manual check.
"""
from __future__ import annotations

import re
import sqlite3
from unittest.mock import MagicMock, Mock

import pytest
from fastapi.testclient import TestClient

from transitos.mocks.clock import SimClock
from transitos.mocks.databricks_maintenance import (
    ASSET_COLUMNS,
    PM_COLUMNS,
    WO_COLUMNS,
    MaintenanceRepository,
    Settings,
    Warehouse,
    WarehouseUnavailable,
    create_app,
)

H = {"X-Agency": "agency-042", "Idempotency-Key": "run-1:create_work_order:1"}
BODY = {"asset": "1106", "description": "Synthetic API connection test", "opened_by": "system"}


class OfflineWarehouse:
    def __init__(self):
        self.settings = Settings("example.cloud.databricks.com", "/sql/1.0/warehouses/abc")
        self.db = sqlite3.connect(":memory:", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.statements = []
        self.fail_after_merge = False
        self.available = True
        for name, columns in (("assets", ASSET_COLUMNS), ("pm_schedules", PM_COLUMNS),
                              ("work_orders", WO_COLUMNS)):
            self.db.execute(f"CREATE TABLE {name} (agency TEXT, is_simulated BOOLEAN, "
                            + ", ".join(f"`{c}`" for c in columns) + ")")
        self.db.executemany("INSERT INTO assets VALUES (?, ?, ?, ?, ?, ?)", [
            ("agency-042", True, "1106", "standard coach Bus 44", "active", 21700),
            ("agency-042", True, "1107", "retired", "retired", 10000),
            ("agency-077", True, "A2001", "other agency", "active", 20000),
            ("agency-042", False, "REAL", "excluded non-simulated record", "active", 20000),
        ])
        self.db.execute("INSERT INTO pm_schedules VALUES (?, ?, ?, ?, ?, ?, ?)",
                        ("agency-042", True, "1106", "PM-A", 24950, 21700, "2026-01-05T06:00:00Z"))

    def close(self):
        pass  # persists across simulated API restarts

    def query(self, statement, parameters=()):
        if not self.available:
            raise WarehouseUnavailable
        self.statements.append((statement, tuple(parameters)))
        statement = re.sub(r"`workspace`\.`maintenance`\.`(\w+)`", r"\1", statement)
        statement = statement.replace("AS STRING", "AS TEXT")
        if statement.startswith("MERGE"):
            source = re.search(r"USING \((SELECT .*?)\) AS source ON", statement).group(1)
            row = dict(self.db.execute(source, parameters).fetchone())
            prior = self.db.execute("SELECT id FROM work_orders WHERE agency = ? AND id = ?",
                                    (row["agency"], row["id"])).fetchone()
            if prior is None:
                columns = tuple(row)
                self.db.execute(f"INSERT INTO work_orders ({', '.join(columns)}) VALUES "
                                f"({', '.join('?' for _ in columns)})", [row[c] for c in columns])
            if self.fail_after_merge:
                self.fail_after_merge = False
                raise WarehouseUnavailable
            return []
        cursor = self.db.execute(statement, parameters)
        return [dict(row) for row in cursor.fetchall()] if cursor.description else []


@pytest.fixture
def connected():
    warehouse = OfflineWarehouse()
    clock = SimClock()
    repo = MaintenanceRepository(warehouse, clock)
    with TestClient(create_app(repo)) as client:
        yield client, warehouse, clock


def test_reads_are_scoped_and_do_not_write(connected):
    client, warehouse, _ = connected
    assert client.get("/assets").status_code == 400
    rows = client.get("/assets", headers=H).json()
    assert {row["asset"] for row in rows} == {"1106", "1107"}
    assert client.get("/assets/A2001", headers=H).status_code == 404
    assert client.get("/assets/REAL", headers=H).status_code == 404
    assert client.get("/pm-schedules", headers=H).json()[0]["service_code"] == "PM-A"
    assert client.get("/workorders", headers=H).json() == []
    assert all(statement.startswith("SELECT") for statement, _ in warehouse.statements)
    assert client.get("/health").json()["database_checked"] is False


def test_create_replay_restart_and_compensation(connected):
    client, warehouse, clock = connected
    first = client.post("/workorders", headers=H, json=BODY)
    assert first.status_code == 200
    order = first.json()
    assert order["status"] == "open" and order["asset"] == "1106"
    assert client.post("/workorders", headers=H, json=BODY).json() == order
    with TestClient(create_app(MaintenanceRepository(warehouse, clock))) as restarted:
        assert restarted.post("/workorders", headers=H, json=BODY).json() == order
        clock.advance(hours=1)
        cancelled = restarted.post(f"/workorders/{order['id']}/cancel", headers=H).json()
        assert cancelled["status"] == "closed" and cancelled["cancelled"]
        assert cancelled["closed_at"] == clock.iso()
        assert restarted.post(f"/workorders/{order['id']}/cancel", headers=H).json() == cancelled
        assert restarted.post("/workorders", headers=H, json=BODY).json() == cancelled
    assert len(client.get("/workorders", headers=H).json()) == 1


def test_response_loss_after_commit_same_key_recovers(connected):
    client, warehouse, clock = connected
    warehouse.fail_after_merge = True
    response = client.post("/workorders", headers=H, json=BODY)
    assert response.status_code == 503
    assert "may have committed" in response.json()["detail"]
    with TestClient(create_app(MaintenanceRepository(warehouse, clock))) as restarted:
        recovered = restarted.post("/workorders", headers=H, json=BODY)
        assert recovered.status_code == 200
        assert len(restarted.get("/workorders", headers=H).json()) == 1
    assert sum(s.startswith("MERGE") for s, _ in warehouse.statements) == 1


def test_duplicate_key_different_body_refused(connected):
    client, _, _ = connected
    client.post("/workorders", headers=H, json=BODY)
    assert client.post("/workorders", headers=H, json={**BODY, "description": "different"}).status_code == 409
    assert len(client.get("/workorders", headers=H).json()) == 1


@pytest.mark.parametrize("body,headers,status", [
    (BODY, {"X-Agency": "agency-042"}, 400),
    (BODY, {"Idempotency-Key": "key"}, 400),
    ({**BODY, "asset": "missing"}, H, 404),
    ({**BODY, "asset": "1107"}, H, 409),
    ({**BODY, "asset": "A2001"}, H, 404),
    ({**BODY, "asset": "REAL"}, H, 404),
    ({**BODY, "opened_by": "robot"}, H, 422),
])
def test_refusal_never_writes(connected, body, headers, status):
    client, warehouse, _ = connected
    assert client.post("/workorders", headers=headers, json=body).status_code == status
    assert not any(s.startswith("MERGE") for s, _ in warehouse.statements)


def test_agency_scoping_keys_and_sql_parameters(connected):
    client, warehouse, _ = connected
    text = "Fault 'quote'; DROP TABLE assets; --"
    order = client.post("/workorders", headers=H, json={**BODY, "description": text}).json()
    assert order["description"] == text
    other_h = {**H, "X-Agency": "agency-077"}
    other = client.post("/workorders", headers=other_h, json={**BODY, "asset": "A2001"}).json()
    assert other["id"] != order["id"]
    assert client.get(f"/workorders/{order['id']}", headers=other_h).status_code == 404
    assert client.get("/assets", headers={"X-Agency": "agency-042' OR 1=1 --"}).json() == []
    assert all(text not in statement for statement, _ in warehouse.statements)


def test_human_order_can_be_repaired_but_not_compensated(connected):
    client, _, clock = connected
    order = client.post("/workorders", headers=H, json={**BODY, "opened_by": "human"}).json()
    assert client.post(f"/workorders/{order['id']}/cancel", headers=H).status_code == 409
    clock.advance(hours=2)
    closed = client.post(f"/sim/workorders/{order['id']}/close", headers=H).json()
    assert closed["status"] == "closed" and closed["cancelled"] == 0
    assert closed["closed_at"] == clock.iso()
    assert client.get("/workorders", headers=H, params={"status": "closed", "asset": "1106",
                      "updated_since": clock.iso()}).json() == [closed]
    assert client.get("/workorders", headers=H, params={"updated_since": "bad"}).status_code == 422


def test_cloud_outage_does_not_use_local_mock(connected):
    client, warehouse, _ = connected
    warehouse.available = False
    assert client.get("/assets", headers=H).status_code == 503
    assert client.post("/workorders", headers=H, json=BODY).status_code == 503


def test_no_destructive_sim_admin_routes(connected):
    client, _, _ = connected
    for path in ("/sim/reset", "/sim/seed", "/sim/meter", "/pending-repairs"):
        assert client.post(path, headers=H, json={}).status_code == 404


def test_preflight_reads_only(connected):
    _, warehouse, clock = connected
    MaintenanceRepository(warehouse, clock).preflight()
    assert len(warehouse.statements) == 3
    assert all(s.startswith("SELECT") and s.endswith("LIMIT 0") for s, _ in warehouse.statements)


def test_existing_tool_adapter_reads_writes_and_honors_kill_switch(connected):
    from transitos.tools.common import Deps, StaticCrosswalk
    from transitos.tools.policy import KillSwitchStore
    from transitos.tools.write_adapters import MaintenanceWriter

    client, warehouse, _ = connected
    crosswalk = StaticCrosswalk()
    crosswalk.add("agency-042", "veh-1", maintenance="1106")
    switch = KillSwitchStore()
    deps = Deps(clients={"maintenance": client}, crosswalk=crosswalk, killswitch=switch)
    writer = MaintenanceWriter(deps)
    switch.set("agency-042", True)
    assert writer.create_work_order("agency-042", "veh-1", BODY["description"], "r:create:1").is_blocked
    assert not any(s.startswith("MERGE") for s, _ in warehouse.statements)
    switch.set("agency-042", False)
    created = writer.create_work_order("agency-042", "veh-1", BODY["description"], "r:create:1")
    assert created.status == "ok" and created.value.asset_vehicle_id == "veh-1"
    replay = writer.create_work_order("agency-042", "veh-1", BODY["description"], "r:create:1")
    assert replay.value.id == created.value.id
    undone = writer.cancel_work_order("agency-042", created.value.id, "r:undo:1")
    assert undone.status == "ok" and undone.value.status == "closed"


def test_warehouse_oauth_named_columns_and_failure_redaction():
    settings = Settings("example.cloud.databricks.com", "/sql/1.0/warehouses/abc")
    connector = MagicMock()
    cursor = connector.return_value.cursor.return_value.__enter__.return_value
    cursor.description = [("name",)]
    cursor.fetchall.return_value = [Mock(asDict=Mock(return_value={"name": "Bus 44"}))]
    warehouse = Warehouse(settings, connector)
    assert warehouse.query("SELECT ? AS name", ["Bus 44"]) == [{"name": "Bus 44"}]
    assert connector.call_args.kwargs["auth_type"] == "databricks-oauth"
    cursor.execute.assert_called_once_with("SELECT ? AS name", parameters=["Bus 44"])
    cursor.execute.side_effect = RuntimeError("sensitive upstream response")
    with pytest.raises(WarehouseUnavailable) as err:
        warehouse.query("SELECT 1")
    assert "sensitive" not in str(err.value) and warehouse.connection is None
    assert connector.return_value.close.called


@pytest.mark.parametrize("catalog", ["workspace; DROP TABLE assets", "x.y", "`bad`"])
def test_invalid_config_identifier_refused(catalog):
    with pytest.raises(ValueError):
        Settings("example.cloud.databricks.com", "/sql/1.0/warehouses/abc", catalog)


def test_missing_configuration_clear_error(monkeypatch):
    monkeypatch.delenv("DATABRICKS_SERVER_HOSTNAME", raising=False)
    with pytest.raises(ValueError, match="Missing connection setting"):
        Settings.from_env()
