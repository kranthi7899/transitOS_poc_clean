"""Postgres profile: the whole system on a real server (TRANSITOS_DB=postgres), crash/redelivery, restart,
schema isolation, server restart. Marked `postgres` (auto-skipped without a server); the server-restart test
is `postgres_admin` (stops/starts the project-local server via scripts/postgres.sh; TRANSITOS_PG_ADMIN=1)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from transitos.contracts.envelope import TOPIC_RAW, TOPIC_RESOLVED
from transitos.integration.world import IntegrationWorld
from transitos.plumbing import pg as pgmod
from transitos.verification.invariants import check_all
from transitos.verification.scenarios import AGENCY, BY_ID

pytestmark = pytest.mark.postgres

ROOT = Path(__file__).resolve().parents[2]
H = {"X-Agency": AGENCY}


class Crash(BaseException):
    """Process killed (not an Exception: nothing in the system may swallow it)."""


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setenv("TRANSITOS_DB", "postgres")
    w = IntegrationWorld()
    w.apply(BY_ID["operator_swap_needed"])
    assert isinstance(w.system.db, pgmod.PgDb)
    yield w
    ns = w.system.pg_namespace()
    w.system.close()
    pgmod.drop_namespace(ns)


def fault_via_mock(w: IntegrationWorld) -> None:
    r = w.mocks.clients["telematics"].post("/sim/fault", headers=H,
                                           json={"vehicle": "T-77", "spn": 520, "fmi": 3, "lamps": {"stop": True}})
    r.raise_for_status()
    w.system.run_until_idle()


def crash_on_commit(s, topic: str, group: str) -> None:
    real = s.bus.commit

    def commit(t, g, offset):
        if (t, g) == (topic, group):
            raise Crash(f"killed before committing {g}")
        real(t, g, offset)
    s.bus.commit = commit


def server_tables(schema: str) -> set[str]:
    psycopg, _ = pgmod._require()
    with psycopg.connect(pgmod.dsn()) as c:
        return {r[0] for r in c.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=%s",
                                        (schema,)).fetchall()}


def test_whole_system_happy_path_on_postgres_with_every_store_in_its_own_schema(world):
    s = world.system
    fault_via_mock(world)
    (run,) = s.store.list_runs()
    rid = run["run_id"]
    world.approve(rid)
    world.advance_clock(48)
    world.close_work_order(rid)
    world.approve(rid, scope="return_to_service")
    assert s.gateway.get_run(rid).status == "DONE"
    assert [w.action for w in s.writes.records] == ["create_work_order", "set_vehicle_unavailable", "reassign_block",
                                                   "assign_operator", "publish_rider_alert", "set_vehicle_available"]
    assert check_all(world.observe()) == []
    ns = s.pg_namespace()
    sch = lambda store: pgmod.schema_name(ns, store)
    assert {"bus_messages", "bus_offsets", "outbox", "xw_entries", "audit_resolved", "decisions"} <= \
        server_tables(sch("plumbing"))
    assert {"runs", "plans", "decisions", "events_seen"} <= server_tables(sch("runs"))
    assert "idem" in server_tables(sch("idem")) and "kill_switch" in server_tables(sch("ks"))
    assert {"checkpoints", "checkpoint_writes"} <= server_tables(sch("ckpt"))
    # the two `decisions` tables (audit vs run index) live in different schemas, as the two SQLite files did
    assert len(s.decisions.list_for_run(rid)) > 3 and s.store.get_decision(rid, 1) is not None


def test_two_systems_are_isolated_by_namespace(monkeypatch):
    monkeypatch.setenv("TRANSITOS_DB", "postgres")
    a, b = IntegrationWorld(), IntegrationWorld()
    a.apply(BY_ID["operator_swap_needed"])
    b.apply(BY_ID["operator_swap_needed"])
    try:
        fault_via_mock(a)
        assert len(a.system.store.list_runs()) == 1 and b.system.store.list_runs() == []
        assert a.system.pg_namespace() != b.system.pg_namespace()
    finally:
        for w in (a, b):
            ns = w.system.pg_namespace()
            w.system.close()
            pgmod.drop_namespace(ns)


def test_restart_rebuilds_from_postgres_and_a_run_awaiting_approval_survives(world):
    s = world.system
    fault_via_mock(world)
    (run,) = s.store.list_runs()
    ns = s.pg_namespace()
    world.restart()                                          # every connection dropped and reopened
    assert world.system.pg_namespace() == ns
    assert world.system.gateway.get_run(run["run_id"]).status == "AWAITING_APPROVAL"
    assert world.system.writes.records == []
    world.approve(run["run_id"])
    assert world.system.writes.records[-1].action == "publish_rider_alert"


def test_workflow_consumer_killed_before_commit_redelivers_and_starter_dedupes(world):
    s = world.system
    crash_on_commit(s, TOPIC_RESOLVED, "workflow")
    with pytest.raises(Crash):
        fault_via_mock(world)
    (run,) = s.store.list_runs()                             # run started, checkpoint durable, offset not
    seen = []
    s.on_resolved = lambda ev, res: seen.append((ev.type, res.action))
    world.restart()
    assert ("VehicleFaultDetected", "duplicate") in seen
    assert [r["run_id"] for r in s.store.list_runs()] == [run["run_id"]]
    assert s.writes.records == []


def test_crosswalk_killed_before_commit_redelivers_without_second_resolved_event(world):
    s = world.system
    crash_on_commit(s, TOPIC_RAW, "crosswalk")
    with pytest.raises(Crash):
        fault_via_mock(world)
    world.restart()
    s = world.system
    resolved = [e for _, _, e in s.bus.messages(TOPIC_RESOLVED) if e.type == "VehicleFaultDetected"]
    assert len(resolved) == 1 and len(s.store.list_runs()) == 1


def test_replay_creates_no_extra_events_runs_or_writes(world):
    s = world.system
    fault_via_mock(world)
    (run,) = s.store.list_runs()
    world.approve(run["run_id"])
    n_res, n_writes = len(s.bus.messages(TOPIC_RESOLVED)), len(s.writes.records)
    s.bus.reset_group(TOPIC_RAW, "crosswalk", 0)
    s.bus.reset_group(TOPIC_RESOLVED, "workflow", 0)
    s.run_until_idle()
    assert len(s.bus.messages(TOPIC_RESOLVED)) == n_res
    assert len(s.store.list_runs()) == 1 and len(s.writes.records) == n_writes


def test_unreachable_server_fails_loudly_and_writes_nothing(monkeypatch):
    monkeypatch.setenv("TRANSITOS_PG", "postgresql://transitos@127.0.0.1:1/transitos")
    with pytest.raises(pgmod.PgError, match="cannot reach Postgres"):
        pgmod.PgDb("t_unreachable_plumbing")


def test_default_profile_is_sqlite_and_bad_values_are_rejected(monkeypatch):
    from transitos.plumbing.storage import db_kind
    monkeypatch.delenv("TRANSITOS_DB", raising=False)
    assert db_kind() == "sqlite" and db_kind("postgres") == "postgres"
    monkeypatch.setenv("TRANSITOS_DB", "mysql")
    with pytest.raises(ValueError, match="TRANSITOS_DB"):
        db_kind()


# ---------------------------------------------------------------- server restart (stops the local server)
def postgres_sh(cmd: str) -> None:
    subprocess.run([str(ROOT / "scripts" / "postgres.sh"), cmd], check=True, capture_output=True, timeout=180)


@pytest.mark.postgres_admin
def test_server_restart_mid_run_loses_no_committed_work(world):
    s = world.system
    fault_via_mock(world)
    (run,) = s.store.list_runs()
    committed = s.bus.committed(TOPIC_RESOLVED, "workflow")
    postgres_sh("down")
    postgres_sh("up")
    world.restart()                                          # new connections against the restarted server
    assert world.system.bus.committed(TOPIC_RESOLVED, "workflow") == committed
    assert world.system.gateway.get_run(run["run_id"]).status == "AWAITING_APPROVAL"
    world.approve(run["run_id"])
    assert [w.action for w in world.system.writes.records][-1] == "publish_rider_alert"
