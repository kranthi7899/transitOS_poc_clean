"""Shared contract suite for the persistence layer, run on SQLite and (when a server is up) Postgres:
Db (tx/execute/script), outbox, relay, crosswalk, append-only audit + DecisionStore, the tool idempotency
store, the kill switch, the run index (one active run per vehicle, plans/decisions upserts, context, signals)
and the LangGraph checkpointer. Same assertions, two backends: the behaviours must be identical.
"""
from __future__ import annotations

import uuid

import pytest

from transitos.contracts.crosswalk import CrosswalkEntry
from transitos.contracts.envelope import CloudEvent
from transitos.contracts.objects import DecisionRecord, WorkOrder
from transitos.contracts.results import Result
from transitos.plumbing import Db, Relay, SqliteBus, SqliteCrosswalk, outbox
from transitos.plumbing import pg as pgmod
from transitos.plumbing.audit import DecisionStore
from transitos.tools.idem_store import IdemStore
from transitos.tools.policy import KillSwitchStore
from transitos.workflow.store import RunsIndex

AG = "ag1"


class Backend:
    """Builds fresh, isolated stores for one backend."""

    def __init__(self, kind: str, tmp_path):
        self.kind, self.tmp, self.ns = kind, tmp_path, f"test_{uuid.uuid4().hex[:10]}"
        self._closers: list = []

    def db(self, name: str = "plumbing"):
        d = Db(str(self.tmp / f"{name}.db")) if self.kind == "sqlite" else pgmod.PgDb(pgmod.schema_name(self.ns, name))
        self._closers.append(d.close)
        return d

    def idem(self):
        if self.kind == "sqlite":
            return IdemStore(self.tmp / "idem.db")
        from transitos.plumbing.pg_stores import PgIdemStore
        s = PgIdemStore(pgmod.schema_name(self.ns, "idem"))
        self._closers.append(s.close)
        return s

    def killswitch(self):
        if self.kind == "sqlite":
            return KillSwitchStore(self.tmp / "ks.db")
        from transitos.plumbing.pg_stores import PgKillSwitchStore
        s = PgKillSwitchStore(pgmod.schema_name(self.ns, "ks"))
        self._closers.append(s.close)
        return s

    def runs(self):
        if self.kind == "sqlite":
            return RunsIndex(str(self.tmp / "runs.db"))
        from transitos.plumbing.pg_stores import PgRunsIndex
        s = PgRunsIndex(pgmod.schema_name(self.ns, "runs"))
        self._closers.append(s.close)
        return s

    def checkpointer(self):
        from transitos.workflow.graph import make_checkpointer
        if self.kind == "sqlite":
            return make_checkpointer(str(self.tmp / "ckpt.db"))
        from transitos.plumbing.pg_stores import make_pg_checkpointer
        s = make_pg_checkpointer(pgmod.schema_name(self.ns, "ckpt"))
        self._closers.append(s.conn.close)
        return s

    def cleanup(self):
        for c in self._closers:
            try:
                c()
            except Exception:  # noqa: BLE001,S110
                pass
        if self.kind == "postgres":
            pgmod.drop_namespace(self.ns)


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.postgres)])
def backend(request, tmp_path):
    b = Backend(request.param, tmp_path)
    yield b
    b.cleanup()


def ev(i: str) -> CloudEvent:
    return CloudEvent(id=i, source=f"adapter/telematics/{AG}", type="VehicleFaultDetected",
                      time="2026-01-01T00:00:00Z", agencyid=AG, data={"native_vehicle_id": "T-1"})


# ---------------------------------------------------------------- Db: transactions and result shapes
def test_tx_commits_on_success_and_rolls_back_on_any_exception(backend):
    db = backend.db()
    db.script("CREATE TABLE IF NOT EXISTS t(k TEXT PRIMARY KEY, v TEXT)")
    with db.tx() as c:
        c.execute("INSERT INTO t VALUES(?,?)", ("a", "1"))
    with pytest.raises(RuntimeError), db.tx() as c:
        c.execute("INSERT INTO t VALUES(?,?)", ("b", "2"))
        raise RuntimeError("boom")
    assert [r["k"] for r in db.execute("SELECT * FROM t ORDER BY k").fetchall()] == ["a"]


def test_rows_are_name_addressable_and_star_hides_the_implicit_rowid(backend):
    db = backend.db()
    db.script("CREATE TABLE IF NOT EXISTS t(k TEXT PRIMARY KEY, v TEXT)")
    db.execute("INSERT INTO t VALUES(?,?)", ("a", "1"))
    row = db.execute("SELECT * FROM t").fetchone()
    assert dict(row) == {"k": "a", "v": "1"}
    assert db.execute("SELECT * FROM t WHERE k=?", ("zzz",)).fetchone() is None
    assert db.execute("SELECT * FROM t WHERE k=?", ("zzz",)).fetchall() == []
    assert db.execute("INSERT OR IGNORE INTO t VALUES(?,?)", ("a", "2")).rowcount == 0
    assert db.execute("INSERT OR IGNORE INTO t VALUES(?,?)", ("b", "2")).rowcount == 1
    assert db.execute("SELECT v FROM t WHERE k=?", ("a",)).fetchone()["v"] == "1"


# ---------------------------------------------------------------- outbox + relay
def test_outbox_dedupes_by_event_id_and_is_part_of_the_callers_transaction(backend):
    db = backend.db()
    outbox.ensure_schema(db)
    with db.tx() as c:
        assert outbox.add(c, ev("e1"), "events.raw", "k") is True
        assert outbox.add(c, ev("e1"), "events.raw", "k") is False          # repeat dropped
        assert outbox.add(c, ev("e1"), "events.resolved", "k", dedupe_id="resolved:e1") is True
    with pytest.raises(RuntimeError), db.tx() as c:
        outbox.add(c, ev("e2"), "events.raw", "k")
        raise RuntimeError("rolled back with the business write")
    rows = db.execute("SELECT event_id, topic FROM outbox ORDER BY seq").fetchall()
    assert [(r["event_id"], r["topic"]) for r in rows] == [("e1", "events.raw"), ("resolved:e1", "events.resolved")]


def test_relay_publishes_in_order_marks_sent_once_and_survives_a_crash_before_marking(backend):
    db = backend.db()
    bus = SqliteBus(db)
    relay = Relay(db, bus)
    with db.tx() as c:
        for i in range(3):
            outbox.add(c, ev(f"e{i}"), "events.raw", "k")

    def crash():
        raise KeyboardInterrupt("killed after flush, before sent_at")
    relay.after_flush_hook = crash
    with pytest.raises(KeyboardInterrupt):
        relay.run_once()
    relay.after_flush_hook = None
    assert relay.drain() == 3                                              # republished: at-least-once
    assert relay.drain() == 0
    assert [e.id for _, _, e in bus.messages("events.raw")] == ["e0", "e1", "e2"] * 2
    sent = db.execute("SELECT sent_at FROM outbox").fetchall()
    assert all(r["sent_at"] and len(r["sent_at"]) == 19 for r in sent)    # 'YYYY-MM-DD HH:MM:SS' on both


# ---------------------------------------------------------------- crosswalk
def test_crosswalk_upsert_lookup_and_parked_events_pop_in_arrival_order(backend):
    db = backend.db()
    xw = SqliteCrosswalk(db)
    with db.tx() as c:
        for i in range(3):
            xw.add_unresolved_in(c, AG, "telematics", "T-9", ev(f"p{i}"))
    xw.add_unresolved(AG, "telematics", "T-9", "p-late")                    # no body: allowed
    assert xw.lookup(AG, "telematics", "T-9") is None
    assert len(xw.unresolved(AG)) == 4 and xw.unresolved("other") == []
    parked = xw.resolve(CrosswalkEntry(agency=AG, product="telematics", native_id="T-9", entity="vehicle",
                                       canonical_id="veh-9"))
    assert [e.id for e in parked] == ["p0", "p1", "p2"]                     # bodyless one cannot be recovered
    assert xw.unresolved() == [] and xw.lookup(AG, "telematics", "T-9") == "veh-9"
    xw.resolve(CrosswalkEntry(agency=AG, product="telematics", native_id="T-9", entity="vehicle", canonical_id="veh-10"))
    assert xw.lookup(AG, "telematics", "T-9") == "veh-10"                   # re-mapping overwrites
    xw.resolve(CrosswalkEntry(agency=AG, product="maintenance", native_id="M-9", entity="vehicle", canonical_id="veh-10"))
    assert xw.all_ids(AG, "veh-10") == {"telematics": "T-9", "maintenance": "M-9"}


# ---------------------------------------------------------------- audit
def _rec(run_id="r1", v=1, outcome="pending") -> DecisionRecord:
    return DecisionRecord(run_id=run_id, plan_version=v, trigger_event_id="e1", outcome=outcome)


def test_decision_store_appends_revisions_and_refuses_update_and_delete(backend):
    db = backend.db()
    ds = DecisionStore(db)
    ds.record_decision(_rec(v=1))
    ds.record_decision(_rec(v=1, outcome="done"))
    ds.record_decision(_rec(run_id="r2"))
    assert [(r.plan_version, r.outcome) for r in ds.list_for_run("r1")] == [(1, "pending"), (1, "done")]
    with pytest.raises(Exception, match="append-only"):
        db.execute("DELETE FROM decisions")
    with pytest.raises(Exception, match="append-only"):
        db.execute("UPDATE decisions SET outcome='done'")
    assert len(ds.list_for_run("r1")) == 2
    DecisionStore(db)                                                       # schema setup is idempotent


# ---------------------------------------------------------------- tool idempotency store + kill switch
def test_idem_store_first_success_wins_and_failures_are_not_recorded(backend):
    s = backend.idem()
    wo1 = Result[WorkOrder](status="ok", value=WorkOrder(id="wo-1", native_no="W1", asset_vehicle_id="veh-1"))
    wo2 = Result[WorkOrder](status="ok", value=WorkOrder(id="wo-2", native_no="W2", asset_vehicle_id="veh-1"))
    assert s.get(AG, "create_work_order", "k") is None
    s.put(AG, "create_work_order", "k", Result[WorkOrder](status="unavailable", product="maintenance"))
    assert s.get(AG, "create_work_order", "k") is None                      # retryable
    s.put(AG, "create_work_order", "k", wo1)
    s.put(AG, "create_work_order", "k", wo2)
    got = s.get(AG, "create_work_order", "k")
    assert got.status == "ok" and got.value.id == "wo-1"
    assert s.get("other-agency", "create_work_order", "k") is None
    s.put(AG, "publish_rider_alert", "a", Result[dict](status="ok", value={"alert": 1}))
    assert s.get(AG, "publish_rider_alert", "a").value == {"alert": 1}


def test_kill_switch_flag_is_live_and_per_agency(backend):
    ks = backend.killswitch()
    assert ks.get(AG) is False
    ks.set(AG, True)
    assert ks.get(AG) is True and ks.get("other") is False
    ks.set(AG, False)
    assert ks.get(AG) is False


# ---------------------------------------------------------------- run index
def test_runs_index_one_active_run_per_vehicle_and_release(backend):
    s = backend.runs()
    assert s.claim(AG, "veh-1", "run-a", "ev1") == "run-a"
    assert s.claim(AG, "veh-1", "run-b", "ev2") == "run-a"                  # attaches to the active run
    assert s.claim(AG, "veh-2", "run-c", "ev3") == "run-c"
    assert s.active_run(AG, "veh-1")["run_id"] == "run-a"
    s.set_status("run-a", "AWAITING_APPROVAL")
    assert [r["run_id"] for r in s.awaiting_runs()] == ["run-a"]
    s.release("run-a", "DONE")
    assert s.active_run(AG, "veh-1") is None and s.get_run("run-a")["active"] == 0
    assert s.claim(AG, "veh-1", "run-d", "ev4") == "run-d"                  # vehicle free again
    assert [r["run_id"] for r in s.list_runs()] == ["run-a", "run-c", "run-d"]   # insertion order


def test_runs_index_row_shape_is_identical(backend):
    s = backend.runs()
    s.claim(AG, "veh-1", "run-a", "ev1")
    assert set(s.get_run("run-a")) == {"run_id", "agency", "vehicle", "trigger_event_id", "status", "active",
                                       "awaiting_since", "deadline", "stale", "pending_resume", "context"}
    assert s.get_run("nope") is None


def test_runs_index_dedupe_context_signals_plans_decisions_escalations(backend):
    from datetime import datetime
    s = backend.runs()
    assert not s.seen("e1")
    s.mark_seen("e1", "run-a", "started")
    s.mark_seen("e1", "other", "ignored")                                   # first mark wins
    s.mark_seen("e2", "run-a", "attached")
    assert s.seen("e1") and s.seen_events() == [
        {"event_id": "e1", "run_id": "run-a", "action": "started"},
        {"event_id": "e2", "run_id": "run-a", "action": "attached"}]
    assert [e["event_id"] for e in s.seen_events("attached")] == ["e2"]
    s.claim(AG, "veh-1", "run-a", "e1")
    s.add_context("run-a", {"id": "x", "n": 1})
    s.add_context("run-a", {"id": "x", "n": 2})                             # same id: ignored
    s.add_context("run-a", {"id": "y"})
    assert [c["id"] for c in s.context("run-a")] == ["x", "y"]
    assert s.pop_stale("run-a") is False
    s.mark_stale("run-a")
    assert s.pop_stale("run-a") is True and s.pop_stale("run-a") is False
    assert s.pop_pending_resume("run-a") is None
    s.set_pending_resume("run-a", {"decision": "approve"})
    assert s.pop_pending_resume("run-a") == {"decision": "approve"} and s.pop_pending_resume("run-a") is None
    s.set_awaiting("run-a", datetime(2026, 1, 1, 8), datetime(2026, 1, 1, 9))
    assert s.get_run("run-a")["deadline"] == "2026-01-01T09:00:00"
    s.save_plan("run-a", 1, {"steps": [1]})
    s.save_plan("run-a", 1, {"steps": [1, 2]})                              # replace
    s.save_plan("run-a", 2, {"steps": []})
    assert s.get_plan("run-a", 1) == {"steps": [1, 2]} and s.get_plan("run-a", 3) is None
    s.save_decision("run-a", 1, {"outcome": "pending"})
    s.save_decision("run-a", 1, {"outcome": "done"})
    assert s.get_decision("run-a", 1) == {"outcome": "done"}
    s.save_escalation("run-a", {"why": "x"})
    s.save_escalation("run-a", {"why": "y"})
    assert s.get_escalation("run-a") == {"why": "y"} and s.get_escalation("nope") is None


# ---------------------------------------------------------------- LangGraph checkpointer
def test_checkpointer_persists_graph_state_and_interrupts_across_a_new_connection(backend):
    """A tiny graph with an interrupt; state survives dropping the graph and rebuilding it from the same store."""
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import Command, interrupt
    from typing_extensions import TypedDict

    class S(TypedDict):
        n: int
        ok: bool

    def a(s: S) -> dict:
        return {"n": s["n"] + 1}

    def ask(s: S) -> dict:
        return {"ok": interrupt("approve?")}

    def build(saver):
        g = StateGraph(S)
        g.add_node("a", a)
        g.add_node("ask", ask)
        g.add_edge(START, "a")
        g.add_edge("a", "ask")
        g.add_edge("ask", END)
        return g.compile(checkpointer=saver)

    cfg = {"configurable": {"thread_id": "run-1"}}
    first = backend.checkpointer()
    build(first).invoke({"n": 1, "ok": False}, cfg)
    first.conn.close()
    second = backend.checkpointer()                                         # "process restart"
    g2 = build(second)
    snap = g2.get_state(cfg)
    assert snap.values["n"] == 2 and snap.next == ("ask",)
    out = g2.invoke(Command(resume=True), cfg)
    assert out == {"n": 2, "ok": True}
    assert build(backend.checkpointer()).get_state({"configurable": {"thread_id": "other"}}).values == {}
