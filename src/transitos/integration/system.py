"""The integrated TransitOS system, local profile, in one process.

    mock products (Track A, FastAPI apps over TestClient, shared SimClock)
      -> watch adapters (B: pollers + signed telematics webhook + backfill) -> outbox -> relay
      -> SqliteBus events.raw -> crosswalk service (B) -> events.resolved
      -> workflow consumer -> starter.handle_event (D) -> LangGraph run (SqliteSaver checkpoints)
      -> Track C ToolSet (reads/writes against the mocks; idempotency store, kill switch, hard limits)
    panel (E) over real ports: RunGateway (D graph), EventSink (B outbox), AuditReader (B DecisionStore),
    KillSwitchStore (C), UnresolvedReader (B crosswalk).

Bus profile: `bus=` / env TRANSITOS_BUS selects `sqlite` (default, zero infra: SqliteBus in plumbing.db) or
`kafka` (KafkaBus on TRANSITOS_KAFKA, default 127.0.0.1:9092; needs `uv sync --extra kafka` and a broker,
see scripts/kafka.sh). On Kafka each System uses its own topic/group namespace, persisted in
`data_dir/kafka-namespace` so restart() reconnects to the same topics and committed offsets; env
TRANSITOS_KAFKA_NAMESPACE overrides it ("none" = the canonical topic names events.raw/...).

Database profile: `db=` / env TRANSITOS_DB selects `sqlite` (default: files under `data_dir`) or `postgres`
(psycopg on TRANSITOS_PG, default postgresql://transitos@127.0.0.1:54329/transitos; needs `uv sync --extra
postgres` and a server, see scripts/postgres.sh). On Postgres each store (plumbing, runs, idem, ks, ckpt) is a
schema `<namespace>_<store>`; the namespace is per System, persisted in `data_dir/pg-namespace` so restart()
reconnects, and TRANSITOS_PG_NAMESPACE overrides it ("none" = the canonical `transitos`).

Everything that would be "our process" lives in SQLite files under `data_dir` (or the Postgres schemas); `restart()` drops every
in-memory object and rebuilds it from those files (the mock products keep running, as external products
would). `tick()` runs each stage once, in order; `run_until_idle()` repeats until nothing moves.
"""
from __future__ import annotations

import os
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from transitos.contracts.envelope import TOPIC_RAW, TOPIC_RESOLVED, CloudEvent
from transitos.contracts.policy import AgencyPolicy
from transitos.mocks import alerts, maintenance, scheduling, telematics, workforce
from transitos.mocks.chaos import Chaos
from transitos.mocks.clock import SimClock
from transitos.mocks.crosswalk_seed import crosswalk_entries
from transitos.mocks.seed import GENERATED
from transitos.panel.app import create_app as create_panel
from transitos.plumbing import (
    AuditConsumer,
    CrosswalkService,
    Db,
    DecisionStore,
    KafkaBus,
    Relay,
    SqliteBus,
    SqliteCrosswalk,
)
from transitos.plumbing import pg as pgmod
from transitos.plumbing.discovery import discover_from_products
from transitos.plumbing.storage import db_kind
from transitos.plumbing.watch import (
    MaintenancePoller,
    SchedulingPoller,
    TelematicsBackfillPoller,
    WebhookReceiver,
    WorkforcePoller,
)
from transitos.tools.idem_store import IdemStore
from transitos.tools.policy import CONFIG_DIR as TOOLS_CONFIG_DIR
from transitos.tools.policy import KillSwitchStore, load_policy
from transitos.tools.registry import Registry
from transitos.verification.model import AuditEvent
from transitos.workflow.graph import Deps, build_graph, make_checkpointer, run_config
from transitos.workflow.limits import HardLimits
from transitos.workflow.reasoning import Reasoner, StubReasoner
from transitos.workflow.starter import HandleResult, handle_event
from transitos.workflow.timers import expire_approvals

from .ports import (
    AuditedRunsIndex,
    CrosswalkUnresolved,
    GraphRunGateway,
    KillSwitchPort,
    OutboxEventSink,
    StoreAuditReader,
)
from .recording import Seq, WorkflowTools, WriteLog

MOCK_MODULES = {"scheduling": scheduling, "telematics": telematics, "maintenance": maintenance,
                "workforce": workforce, "alerts": alerts}
WRITE_PRODUCTS = ("maintenance", "scheduling", "workforce", "alerts")
INTEGRATION_CONFIG = Path(__file__).parent / "config"
WORKFLOW_GROUP = "workflow"
WEBHOOK_URL = "http://transitos/webhook"
BUS_KINDS = ("sqlite", "kafka")


def bus_kind(explicit: str | None = None) -> str:
    kind = (explicit or os.environ.get("TRANSITOS_BUS") or "sqlite").strip().lower()
    if kind not in BUS_KINDS:
        raise ValueError(f"TRANSITOS_BUS must be one of {BUS_KINDS}, got {kind!r}")
    return kind


@dataclass
class Mocks:
    """The five SIMULATED products (external to our process: they survive System.restart())."""
    clock: SimClock
    apps: dict[str, FastAPI] = field(default_factory=dict)
    chaos: dict[str, Chaos] = field(default_factory=dict)
    clients: dict[str, TestClient] = field(default_factory=dict)
    webhook: Callable[[str, bytes, dict], None] | None = None   # telematics outbound webhook target

    @classmethod
    def start(cls, clock: SimClock) -> Mocks:
        m = cls(clock)
        for name, mod in MOCK_MODULES.items():
            m.chaos[name] = Chaos()
            kw = {"sender": m._send} if name == "telematics" else {}
            m.apps[name] = mod.create_app(clock, m.chaos[name], None, **kw)
            m.clients[name] = TestClient(m.apps[name], base_url=f"http://{name}")
        return m

    def _send(self, url: str, body: bytes, headers: dict) -> None:
        if self.webhook is None:
            raise ConnectionError("no webhook receiver")  # mock keeps the event and retries later
        self.webhook(url, body, headers)

    def seed(self, agency: str) -> None:
        for c in self.clients.values():
            c.post("/sim/reset", headers={"X-Agency": agency})
            r = c.post("/sim/seed", headers={"X-Agency": agency})
            r.raise_for_status()
        self.clients["telematics"].post("/sim/webhook", json={"url": WEBHOOK_URL}, headers={"X-Agency": agency})


class System:
    def __init__(self, agency: str, mocks: Mocks, data_dir: str | Path | None = None, *,
                 reasoner: Reasoner | None = None, seed_crosswalk: bool = True, bus: str | None = None,
                 db: str | None = None):
        self.agency, self.mocks, self.clock = agency, mocks, mocks.clock
        self.dir = Path(data_dir or tempfile.mkdtemp(prefix="transitos-"))
        self.dir.mkdir(parents=True, exist_ok=True)
        self.bus_kind = bus_kind(bus)
        self.db_kind = db_kind(db)
        self.config_dir = INTEGRATION_CONFIG if (INTEGRATION_CONFIG / f"{agency}.yaml").exists() else TOOLS_CONFIG_DIR
        self.reasoner = reasoner or StubReasoner()
        self.policy_patch: dict[str, Any] = {}   # scenario overrides (e.g. vehicle_fit), merged into policy()
        self.seq = Seq()
        self.writes = WriteLog(self.seq, lambda: [r["run_id"] for r in self.store.list_runs()])
        self.on_resolved: Callable[[CloudEvent, HandleResult], None] | None = None  # narration hook
        # ordered operations log: human decisions, approval expiries, kill-switch toggles (shares `seq`
        # with the write log; Track F's AuditEvent shape)
        self.ops_log: list[AuditEvent] = []
        self._build()
        self.discovery: list[dict] = []   # Phase 10 Step 2: crosswalk worked out from product reads
        if seed_crosswalk:
            for e in crosswalk_entries(agency, self.clock.now()):
                self.xw.resolve(e)
            if agency in GENERATED:
                self.discovery = discover_from_products(self.xw, mocks.clients, agency, self.clock.now())
        mocks.webhook = self._webhook

    # ------------------------------------------------------------------ construction / restart
    def _build(self) -> None:
        d = self.dir
        pg = self.db_kind == "postgres"
        ns = self.pg_namespace() if pg else ""
        self.db = pgmod.PgDb(pgmod.schema_name(ns, "plumbing")) if pg else \
            Db(str(d / "plumbing.db"))            # bus, outbox, adapter cursors, crosswalk, audit
        self.bus: SqliteBus | KafkaBus = SqliteBus(self.db) if self.bus_kind == "sqlite" else \
            KafkaBus(namespace=self.kafka_namespace())
        self.relay = Relay(self.db, self.bus)
        self.xw = SqliteCrosswalk(self.db, self.bus)
        self.xw_svc = CrosswalkService(self.db, self.bus, self.xw)
        c, a = self.mocks.clients, self.agency
        self.pollers = [MaintenancePoller(self.db, c["maintenance"], a), SchedulingPoller(self.db, c["scheduling"], a),
                        WorkforcePoller(self.db, c["workforce"], a), TelematicsBackfillPoller(self.db, c["telematics"], a)]
        self.webhook = WebhookReceiver(self.db, a)
        self.webhook_client = TestClient(self.webhook.app(), base_url="http://transitos")
        self.audit_consumers = [AuditConsumer(self.db, self.bus, "raw"), AuditConsumer(self.db, self.bus, "resolved")]
        self.decisions = DecisionStore(self.db)
        if pg:
            from transitos.plumbing.pg_stores import PgIdemStore, PgKillSwitchStore
            self.idem = PgIdemStore(pgmod.schema_name(ns, "idem"))
            self.killswitch = PgKillSwitchStore(pgmod.schema_name(ns, "ks"))
        else:
            self.idem = IdemStore(d / "idem.db")
            self.killswitch = KillSwitchStore(d / "killswitch.db")
        self.registry = Registry(self.config_dir, clients={p: c[p] for p in WRITE_PRODUCTS}, crosswalk=self.xw,
                                 idem=self.idem, killswitch=self.killswitch, sleep=lambda s: None,
                                 policy_for=lambda ag: self.policy() if ag == self.agency else load_policy(ag, self.config_dir))
        self.toolset = self.registry.toolset(a)
        if pg:
            from transitos.plumbing.pg_stores import PgAuditedRunsIndex
            self.store = PgAuditedRunsIndex(pgmod.schema_name(ns, "runs"), self.decisions)
        else:
            self.store = AuditedRunsIndex(str(d / "runs.db"), self.decisions)
        self.deps = Deps(tools=WorkflowTools(self.toolset, self.writes), policy=self.policy,
                         limits=HardLimits(self.policy().limits), store=self.store, clock=self.clock.now,
                         reasoner=self.reasoner)
        if pg:
            from transitos.plumbing.pg_stores import make_pg_checkpointer
            self._ckpt = make_pg_checkpointer(pgmod.schema_name(ns, "ckpt"))
        else:
            self._ckpt = make_checkpointer(str(d / "checkpoints.db"))
        self.graph = build_graph(self.deps, self._ckpt)
        self.gateway = GraphRunGateway(self)
        self.panel = create_panel(gateway=self.gateway, crosswalk=self.xw, events=OutboxEventSink(self),
                                  audit=StoreAuditReader(self), killswitch=KillSwitchPort(self.killswitch, self._log_ks),
                                  unresolved=CrosswalkUnresolved(self), agencies=[a], clock=self.clock.now)
        self.panel_client = TestClient(self.panel)

    def kafka_namespace(self) -> str:
        env = os.environ.get("TRANSITOS_KAFKA_NAMESPACE")
        if env is not None:
            return "" if env.strip().lower() in ("", "none") else env.strip()
        f = self.dir / "kafka-namespace"
        if not f.exists():
            f.write_text(f"transitos-{uuid.uuid4().hex[:12]}")
        return f.read_text().strip()

    def pg_namespace(self) -> str:
        env = pgmod.namespace_from_env()
        if env is not None:
            return env
        f = self.dir / "pg-namespace"
        if not f.exists():
            f.write_text(pgmod.new_namespace())
        return f.read_text().strip()

    def _extra_closers(self) -> list[Callable[[], None]]:
        """Postgres-backed stores own connections of their own (the SQLite ones are left to the GC, as before)."""
        return [x.close for x in (self.idem, self.killswitch) if self.db_kind == "postgres"]

    def close(self) -> None:
        """Release every connection and bus client (end of life; restart() is the crash-and-rebuild path)."""
        for closer in (self.bus.close if isinstance(self.bus, KafkaBus) else None, self.db.close,
                       self._ckpt.conn.close, self.store.close, *self._extra_closers()):
            if closer is None:
                continue
            try:
                closer()
            except Exception:  # noqa: BLE001,S110 - best effort
                pass

    def restart(self) -> None:
        """Simulate a process restart: close every connection, rebuild every in-process object from the
        same SQLite files, then let the workflow recover (re-drive runs that died between checkpoints)."""
        # a Kafka bus is closed without committing anything: uncommitted deliveries come back, as after a crash
        kafka_close = self.bus.close if isinstance(self.bus, KafkaBus) else None
        for closer in (kafka_close, self.db.close, self._ckpt.conn.close, self.store.close,
                       *self._extra_closers()):
            if closer is None:
                continue
            try:
                closer()
            except Exception:  # noqa: BLE001,S110 - a crashed process may leave a broken connection
                pass
        self._build()
        self.recover()

    def recover(self) -> list[str]:
        """Continue active runs whose last checkpoint is not an interrupt (crash mid-node). Interrupted runs
        (awaiting approval / repair / return) just wait. Uncommitted bus offsets are redelivered by tick()."""
        resumed = []
        for r in self.store.list_runs():
            if not r["active"]:
                continue
            snap = self.graph.get_state(run_config(r["run_id"]))
            if snap.next and not any(t.interrupts for t in snap.tasks):
                self.graph.invoke(None, run_config(r["run_id"]))
                resumed.append(r["run_id"])
        return resumed

    # ------------------------------------------------------------------ policy (file + live kill switch)
    def policy(self) -> AgencyPolicy:
        p = load_policy(self.agency, self.config_dir)
        if self.policy_patch:
            p = p.model_copy(update=self.policy_patch)
        if self.killswitch.get(self.agency):
            p = p.model_copy(update={"kill_switch": True})
        return p

    def log_human(self, kind: str, run_id: str | None, plan_version: int | None = None, **data: Any) -> None:
        self.ops_log.append(AuditEvent(seq=self.seq.next(), kind=kind, run_id=run_id,  # type: ignore[arg-type]
                                       plan_version=plan_version, data=data))

    def _log_ks(self, agency: str, on: bool) -> None:
        if agency == self.agency:
            self.log_human("kill_switch", None, on=on)

    # ------------------------------------------------------------------ ingress
    def _webhook(self, url: str, body: bytes, headers: dict) -> None:
        r = self.webhook_client.post("/webhook", content=body, headers=headers)
        if r.status_code >= 500:
            raise ConnectionError(f"webhook {r.status_code}")

    def publish_raw(self, event: CloudEvent, key: str | None = None) -> None:
        """Put an event on events.raw as a product adapter would (at-least-once: repeats are allowed)."""
        from transitos.contracts.events import PUBLISHER
        from transitos.plumbing.crosswalk_service import NATIVE_FIELD
        f, _ = NATIVE_FIELD[event.type]
        self.bus.send(TOPIC_RAW, key or f"{PUBLISHER[event.type]}:{event.data.get(f, '')}", event)
        self.bus.flush()   # acknowledged before we return (a no-op on SqliteBus)

    # ------------------------------------------------------------------ the loop
    def consume_resolved(self, limit: int = 100) -> int:
        n = 0
        for offset, ev in self.bus.poll(TOPIC_RESOLVED, WORKFLOW_GROUP, limit):
            if ev.agencyid == self.agency:
                res = handle_event(ev, self.store, self.graph, self.clock.now)
                if self.on_resolved:
                    self.on_resolved(ev, res)
            self.bus.commit(TOPIC_RESOLVED, WORKFLOW_GROUP, offset)  # after the run's checkpoint is durable
            n += 1
        return n

    def tick(self) -> int:
        """One deterministic pass: pollers -> relay -> crosswalk -> audit -> workflow -> timers.
        Returns the amount of work done (0 = idle)."""
        work = sum(p.poll() for p in self.pollers)
        work += self.relay.drain()
        work += self.xw_svc.drain()
        for a in self.audit_consumers:
            a.run_once()
        work += self.consume_resolved()
        now = self.clock.now()
        for r in self.store.awaiting_runs():  # logged before the timer resumes the run
            if r["deadline"] and datetime.fromisoformat(r["deadline"]) <= now:
                self.log_human("expiry", r["run_id"], None, by="timer")
        expired = expire_approvals(self.graph, self.store, self.clock.now)
        return work + len(expired)

    def run_until_idle(self, max_ticks: int = 50) -> int:
        total = 0
        for _ in range(max_ticks):
            n = self.tick()
            total += n
            if n == 0:
                return total
        raise RuntimeError(f"system not idle after {max_ticks} ticks")

    # ------------------------------------------------------------------ convenience
    def run_state(self, run_id: str) -> dict[str, Any] | None:
        snap = self.graph.get_state(run_config(run_id))
        return snap.values.get("run") if snap.values else None

    def runs(self) -> list[dict[str, Any]]:
        return [r for r in (self.run_state(x["run_id"]) for x in self.store.list_runs()) if r]
