"""Track F's `World` over the REAL integrated system (`make_world()` is what the e2e tests and
`python -m transitos.verification.report --world transitos.integration.world:make_world` load).

How each World verb maps onto the real system:
  apply        fresh SimClock (scenario T0) + five mock products seeded with the scenario baseline fleet
               (transitos.integration.fleets) + fresh System on new SQLite files; tweaks/chaos applied;
               baseline events (e.g. a human WO) consumed so the scenario starts quiescent
  deliver      event onto events.raw (as a product adapter would; repeats allowed) -> run_until_idle
  approve      panel HTTP POST /runs/{id}/decision (scope plan | return_to_service) -> run_until_idle
  reject       panel decision reject
  advance      SimClock.advance + telematics flush -> run_until_idle (approval timers fire)
  kill switch  panel HTTP POST /killswitch
  close_wo     maintenance mock closes the run's WO (/sim) -> poller emits WorkOrderClosed -> resume
  take_spare   scheduling mock: a dispatcher reserves the proposed spare (/sim/vehicle-status)
  restart      (extra, not in F's Protocol) System.restart(): rebuild from the same SQLite files

Observation: writes = the integration write log (every call that reached a Track C adapter);
audit = the system's ordered operations log (decisions accepted by the panel's RunGateway, approval
expiries from the timer, kill-switch toggles from the panel port), plus one `write` event per action key
found in Track B's append-only DecisionStore (the real audit of what the workflow did).
Final vehicles/blocks/WOs/alerts are read straight from the mock products' stores (god's-eye view,
unaffected by chaos).
"""
from __future__ import annotations

import json
import tempfile
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace

from transitos.contracts.envelope import TOPIC_FAILED, CloudEvent
from transitos.contracts.objects import Block, Operator, RunState, Vehicle, WorkOrder
from transitos.mocks.clock import SimClock, iso
from transitos.mocks.seed import build_fleet, register_fleet
from transitos.tools.read_adapters import map_block, map_vehicle, map_work_order
from transitos.verification.latency import detection_latency
from transitos.verification.model import AuditEvent, Observation, WriteRecord
from transitos.verification.scenarios import AGENCY, NATIVE_ASSET, T0, Scenario
from transitos.world.engine import WorldEngine
from transitos.world.lag import ProductLag
from transitos.world.ports import MockPorts
from transitos.world.script import hhmm_on
from transitos.world.truth import GroundTruth

from .fleets import scenario_fleet
from .system import Mocks, System

# which mock product a write action lands on (for per-action reject_write chaos)
ACTION_PRODUCT = {"set_vehicle_unavailable": "scheduling", "set_vehicle_available": "scheduling",
                  "reassign_block": "scheduling", "assign_operator": "workforce"}


class IntegrationWorld:
    def __init__(self) -> None:
        self.system: System | None = None
        self._acks: dict[str, list[str]] = {}
        self.engine: WorldEngine | None = None          # Phase 10 Step 3: set when the scenario has a world_script
        self.latency_audit: list[AuditEvent] = []

    # ------------------------------------------------------------------ setup
    def apply(self, scenario: Scenario) -> None:
        t = scenario.tweaks
        if self.system is not None:   # one world, one live system: release the previous one's clients
            self.system.close()
        register_fleet(AGENCY, scenario_fleet(t))
        self.mocks = Mocks.start(SimClock(T0))
        self.mocks.seed(AGENCY)
        self.system = s = System(AGENCY, self.mocks, tempfile.mkdtemp(prefix="transitos-world-"))
        self._acks = {}
        s.policy_patch = {"vehicle_fit": fit} if (fit := getattr(t, "vehicle_fit", "allow_larger")) != "allow_larger" else {}
        if drop := getattr(t, "drop_operator_fields", ()):   # a product that holds only part of the operator data
            self.mocks.clients["workforce"].post("/sim/operator-fields", json={"drop": list(drop)},
                                                 headers=self._h).raise_for_status()
        self.chaos = dict(scenario.chaos)
        if t.human_wo_exists:
            r = self.mocks.clients["maintenance"].post(
                "/workorders", json={"asset": NATIVE_ASSET, "description": "brake check", "opened_by": "human"},
                headers=self._h)
            r.raise_for_status()
        s.run_until_idle()  # baseline consumed before the scenario's first event
        self._install_chaos()
        self.engine, self.latency_audit = None, []
        if (day := getattr(t, "world_script", None)) is not None:
            self._start_world(day, getattr(t, "world_lag", None))

    # ------------------------------------------------------------------ world engine (Phase 10 Step 3)
    def _start_world(self, day, lag: ProductLag | None) -> None:
        """Ground truth + per-product lag drive the real mocks; the system runs to idle after every step and each
        step stamps what it saw with the sim time (proposals, writes) for the latency measurement (A99)."""
        s = self.system
        run_idle, seen_plans, n_writes = s.run_until_idle, set(), [0]

        def stamped_idle(*a, **k):
            n = run_idle(*a, **k)
            at = iso(s.clock.now())
            for r in s.runs():
                run = RunState.model_validate(r)
                if run.plan is not None and (run.run_id, run.plan_version) not in seen_plans:
                    seen_plans.add((run.run_id, run.plan_version))
                    self.latency_audit.append(AuditEvent(seq=len(self.latency_audit), kind="proposal",
                                                         run_id=run.run_id, plan_version=run.plan_version,
                                                         data={"at": at}))
            for rec in s.writes.records[n_writes[0]:]:
                self.latency_audit.append(AuditEvent(seq=len(self.latency_audit), kind="write", run_id=rec.run_id,
                                                     key=rec.key, data={"action": rec.action, "at": at, "ok": rec.ok,
                                                                        "compensation": rec.compensation}))
            n_writes[0] = len(s.writes.records)
            return n
        s.run_until_idle = stamped_idle  # type: ignore[method-assign]
        clock = s.clock
        self.engine = WorldEngine(GroundTruth(clock, day, seed=0), lag or ProductLag(),
                                  MockPorts(self.mocks, AGENCY, build_fleet(AGENCY, clock.now())), clock,
                                  settle=s.run_until_idle)

    def advance_world_to(self, hhmm: str) -> None:
        """Script op ("world_until", "HH:MM"): run the world engine to that time of day on the scenario day."""
        if self.engine is None:
            raise ValueError("scenario has no world_script")
        self.engine.run_until(hhmm_on(T0, hhmm))

    def latency(self) -> dict | None:
        return None if self.engine is None else detection_latency(self.engine.observations_log, self.latency_audit)

    def timeline_json(self) -> str:
        """Everything the world did and every product ended up holding, canonical JSON (byte-compare across runs)."""
        eng = self.engine
        doc = {
            "observations": eng.timeline() if eng else [],
            "skipped": [k.to_dict() for k in eng.skipped] if eng else [],
            "latency_audit": [a.model_dump(mode="json") for a in self.latency_audit],
            "latency": self.latency(),
            "writes": [w.model_dump(mode="json") for w in self.system.writes.records],
            "products": {"telematics": self._docs("telematics", "events"),
                         "scheduling": self._docs("scheduling", "changes"),
                         "maintenance": self._docs("maintenance", "wo"),
                         "workforce": self._docs("workforce", "changes")},
        }
        return json.dumps(doc, sort_keys=True, separators=(",", ":"), default=str)

    @property
    def _h(self) -> dict:
        return {"X-Agency": AGENCY}

    def _install_chaos(self) -> None:
        s, ch = self.system, self.chaos
        assert s is not None
        for product, mode in ch.items():
            if product in self.mocks.chaos and mode == "timeout":
                self.mocks.chaos[product].set("timeout")
        if ch.get("maintenance") == "duplicate":   # a retried write reaches the product twice, same key
            s.writes.repeat = lambda action: 2 if action == "create_work_order" else 1
        if (target := ch.get("reject_write")) is not None:
            @contextmanager
            def around(action: str, key: str, comp: bool) -> Iterator[None]:
                product = ACTION_PRODUCT.get(action)
                on = action == target and not comp and product is not None
                if on:
                    self.mocks.chaos[product].set("reject_write")
                try:
                    yield
                finally:
                    if on:
                        self.mocks.chaos[product].set("reject_write", False)
            s.writes.around = around
        if (n := ch.get("kill_after_writes")) is not None:
            def after(rec: WriteRecord, _res) -> None:
                done = [w for w in s.writes.records if w.ok and not w.compensation and w.run_id == rec.run_id]
                if rec.ok and not rec.compensation and len(done) == n:
                    self.set_kill_switch(True)    # an operator hits the panel kill switch mid-run
            s.writes.after = after

    # ------------------------------------------------------------------ verbs
    def deliver(self, event: CloudEvent) -> None:
        self.system.publish_raw(event)
        self.system.run_until_idle()

    def acknowledge(self, run_id: str, rules: list[str]) -> None:
        self._acks.setdefault(run_id, []).extend(rules)

    def _decide(self, run_id: str, decision: str, scope: str, by: str) -> None:
        s = self.system
        run = s.gateway.get_run(run_id)
        if run is None:
            return
        # the panel's RunGateway logs the accepted decision (ordered before any write it causes); the panel
        # itself answers 409 when a requires_ack clearance item is not acknowledged
        s.panel_client.post(f"/runs/{run_id}/decision", json={
            "decision": decision, "plan_version": run.plan_version, "user": by, "scope": scope,
            "acknowledged": sorted(set(self._acks.get(run_id, [])))})
        s.run_until_idle()

    def approve(self, run_id: str, scope: str = "plan", by: str = "dispatcher") -> None:
        self._decide(run_id, "approve", scope, by)

    def reject(self, run_id: str, by: str = "dispatcher") -> None:
        self._decide(run_id, "reject", "plan", by)

    def advance_clock(self, hours: float) -> None:
        self.system.clock.advance(hours=hours)
        self.mocks.clients["telematics"].post("/sim/flush", headers=self._h)
        self.system.run_until_idle()  # approval timers fire (logged as expiry by the system)

    def set_kill_switch(self, on: bool) -> None:
        r = self.system.panel_client.post("/killswitch", json={"agency": AGENCY, "on": on})
        r.raise_for_status()

    def close_work_order(self, run_id: str) -> None:
        run = self.system.gateway.get_run(run_id)
        if run is None or not run.wo_id:
            return
        self.mocks.clients["maintenance"].post(f"/sim/workorders/{run.wo_id}/close", headers=self._h)
        self.system.run_until_idle()

    def take_spare(self, run_id: str) -> None:
        run = self.system.gateway.get_run(run_id)
        spare = next((st.params["to_vehicle_id"] for st in (run.plan.steps if run and run.plan else [])
                      if st.action == "reassign_block"), None)
        if spare is None:
            return
        native = self.system.xw.all_ids(AGENCY, spare)["scheduling"]
        self.mocks.clients["scheduling"].post("/sim/vehicle-status", headers=self._h,
                                              json={"vehicle": native, "status": "reserved", "changed_by": "dispatcher"})
        self.system.run_until_idle()

    def pick(self, canonical_id: str) -> None:
        """Phase 10 Step 2: the human picks an identity in the panel (GET /unresolved, POST /crosswalk); the
        panel republishes the parked events, which then resolve."""
        items = self.system.panel_client.get("/unresolved", params={"agency": AGENCY}).json()
        if not items:
            return
        i = items[0]
        r = self.system.panel_client.post("/crosswalk", json={
            "agency": i["agency"], "product": i["product"], "native_id": i["native_id"], "entity": "vehicle",
            "canonical_id": canonical_id})
        r.raise_for_status()
        self.system.run_until_idle()

    def restart(self) -> None:
        """Process restart (not in F's World Protocol): rebuild from the same SQLite files, recover."""
        self.system.restart()
        self.system.run_until_idle()

    # ------------------------------------------------------------------ observation
    def _docs(self, product: str, coll: str) -> list[dict]:
        return self.mocks.apps[product].state.ctx.store.all(AGENCY, coll)

    def observe(self) -> Observation:
        s = self.system
        xw = SimpleNamespace(crosswalk=s.xw)
        runs = [RunState.model_validate(r) for r in s.runs()]
        writes = list(s.writes.records)
        first_seq = {}
        for w in writes:
            first_seq.setdefault((w.run_id, w.key), w.seq)
        audit = list(s.ops_log)
        for r in runs:
            keys = {a["key"] for rec in s.decisions.list_for_run(r.run_id) for a in rec.actions if a.get("key")}
            for k in sorted(keys):
                audit.append(AuditEvent(seq=first_seq.get((r.run_id, k), 0), kind="write", run_id=r.run_id, key=k))
        assign = {d["block"]: d["operator"] for d in self._docs("workforce", "assignments")}
        blocks = []
        for b in self._docs("scheduling", "blocks"):
            blk: Block = map_block(xw, AGENCY, b)  # type: ignore[arg-type]
            if b["id"] in assign:
                blk = blk.model_copy(update={"operator_id": s.xw.lookup(AGENCY, "workforce", assign[b["id"]])})
            blocks.append(blk)
        vehicles: list[Vehicle] = [map_vehicle(xw, AGENCY, v) for v in self._docs("scheduling", "vehicles")]  # type: ignore[arg-type]
        wos: list[WorkOrder] = [map_work_order(xw, AGENCY, w) for w in self._docs("maintenance", "wo")]  # type: ignore[arg-type]
        ops = [Operator(id=s.xw.lookup(AGENCY, "workforce", o["operator"]) or o["operator"],
                        qualifications=[q for q in str(o["qualifications"]).split(";") if q],
                        lift_trained=bool(o["lift_trained"]), hours_today=float(o["hours_today"]),
                        hours_week=float(o["hours_week"]), on_extra_board=bool(o["on_extra_board"]))
               for o in self._docs("workforce", "operators")]
        attached: dict[str, list[str]] = defaultdict(list)
        for row in s.store.seen_events("attached"):
            attached[row["run_id"]].append(row["event_id"])
        dead = [{"event_id": e.id, "reason": e.failurereason, "native": e.data.get("native_vehicle_id")
                 or e.data.get("native_asset_id")} for _, _, e in s.bus.messages(TOPIC_FAILED)]
        return Observation(agency=AGENCY, policy=s.policy(), runs=runs, writes=writes,
                           audit=sorted(audit, key=lambda a: a.seq), vehicles=vehicles, blocks=blocks,
                           operators=ops, work_orders=wos, alerts=self._docs("alerts", "alerts"),
                           dead_letters=dead, attached_events=dict(attached))


def make_world() -> IntegrationWorld:
    """Zero-arg factory Track F loads (tests/e2e/world.py DEFAULT_SPEC)."""
    return IntegrationWorld()
