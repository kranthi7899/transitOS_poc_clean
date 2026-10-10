"""Narrated happy path through the real integrated system (local profile, SIMULATED products):

    uv run python -m transitos.integration.demo        (or: make demo)

fault in the telematics mock -> signed webhook -> outbox -> bus -> crosswalk -> workflow starter ->
LangGraph run -> plan proposed -> approval via the panel -> five writes through Track C adapters
against the mock products -> sim clock advance -> shop closes WO -> WorkOrderClosed -> return-to-service
approval -> done, then the audit trail. Uses the verification baseline fleet (agency-a) with the block's
assigned operator near the hour limit, so the plan needs all five saga steps.
"""
from __future__ import annotations

import sys

from transitos.contracts.envelope import TOPIC_RAW, TOPIC_RESOLVED
from transitos.verification.invariants import check_all
from transitos.verification.scenarios import AGENCY, BY_ID

from .world import IntegrationWorld

H = {"X-Agency": AGENCY}


def out(msg: str = "") -> None:
    print(msg, flush=True)


def stage(n: int, title: str) -> None:
    out(f"\n[{n}] {title}")


def main() -> int:
    w = IntegrationWorld()
    w.apply(BY_ID["operator_swap_needed"])   # baseline fleet seeded into the five mock products
    s = w.system
    resolved_log: list[str] = []
    s.on_resolved = lambda ev, res: resolved_log.append(
        f"      starter: {ev.type} {ev.id} -> {res.action}" + (f" run={res.run_id}" if res.run_id else ""))

    out("TransitOS integration demo - SIMULATED products, local profile (SQLite + in-process bus)")
    out(f"  agency {AGENCY}, sim clock {s.clock.iso()}, state in {s.dir}")

    stage(1, "Fault raised in the telematics mock (stop lamp on BUS-44 / T-77)")
    vendor = w.mocks.clients["telematics"].post(
        "/sim/fault", headers=H, json={"vehicle": "T-77", "spn": 520, "fmi": 3, "lamps": {"stop": True}}).json()
    out(f"  vendor eventId {vendor['eventId']}, pushed to the signed webhook (HMAC sha256)")

    stage(2, "Watch adapter -> outbox -> bus -> crosswalk -> workflow starter")
    s.run_until_idle()
    (raw,) = [e for _, _, e in s.bus.messages(TOPIC_RAW) if e.type == "VehicleFaultDetected"]
    (res,) = [e for _, _, e in s.bus.messages(TOPIC_RESOLVED) if e.id == raw.id]
    out(f"  events.raw      id={raw.id}  key=telematics:{raw.data['native_vehicle_id']}")
    out(f"  crosswalk       T-77 -> {res.canonicalid}  refs={res.data['refs']}")
    out(f"  events.resolved key={res.canonicalid}")
    for line in resolved_log:
        out(line)
    (rid,) = [r["run_id"] for r in s.store.list_runs()]
    run = s.gateway.get_run(rid)

    stage(3, f"Plan proposed (status {run.status}, plan v{run.plan_version}, model {s.decisions.list_for_run(rid)[-1].model})")
    out(f"  decision {run.plan.decision}: {run.plan.reasoning}")
    for st in run.plan.steps:
        out(f"    {st.action:<24} {st.params}")
    out(f"  writes so far: {len(s.writes.records)} (nothing is written before approval)")

    stage(4, "Dispatcher approves in the panel (POST /runs/{id}/decision)")
    r = s.panel_client.post(f"/runs/{rid}/decision",
                            json={"decision": "approve", "plan_version": run.plan_version, "user": "dispatcher"})
    out(f"  panel -> HTTP {r.status_code}, run status {r.json()['status']}")
    s.run_until_idle()

    stage(5, "Five writes through the Track C adapters against the mock products")
    for wr in s.writes.records:
        out(f"  #{wr.seq} {wr.action:<24} ok={wr.ok} effect={wr.effect_id}")
        out(f"       idempotency key {wr.key}")
    blk = w.mocks.clients["scheduling"].get("/blocks", headers=H).json()[0]
    asg = w.mocks.clients["workforce"].get(f"/assignments/{blk['id']}", headers=H).json()
    veh = w.mocks.clients["scheduling"].get("/vehicles/BUS-44", headers=H).json()
    out(f"  product state: BUS-44 {veh['status']}; {blk['id']} now on {blk['vehicle']} driven by {asg['operator']}")
    run = s.gateway.get_run(rid)
    out(f"  run status {run.status}, work order {run.wo_id}")

    stage(6, "Sim clock +48h; the shop closes the work order in the maintenance mock")
    s.clock.advance(hours=48)
    resolved_log.clear()
    w.mocks.clients["maintenance"].post(f"/sim/workorders/{run.wo_id}/close", headers=H)
    s.run_until_idle()
    for line in resolved_log:
        out(line)
    out(f"  run status {s.gateway.get_run(rid).status} (return to service needs its own approval)")

    stage(7, "Return-to-service approved in the panel")
    r = s.panel_client.post(f"/runs/{rid}/decision", json={"decision": "approve", "plan_version": run.plan_version,
                                                            "user": "dispatcher", "scope": "return_to_service"})
    s.run_until_idle()
    last = s.writes.records[-1]
    out(f"  panel -> HTTP {r.status_code}; #{last.seq} {last.action} key {last.key}")
    veh = w.mocks.clients["scheduling"].get("/vehicles/BUS-44", headers=H).json()
    out(f"  BUS-44 {veh['status']}; run status {s.gateway.get_run(rid).status}")

    stage(8, "Audit trail (Track B append-only DecisionStore)")
    for rec in s.panel_client.get(f"/runs/{rid}").json()["history"]:
        out(f"  plan v{rec['plan_version']} outcome={rec['outcome']} model={rec['model']} "
            f"human={rec['human_action'].get('decision')} by {rec['human_action'].get('by')}")
        for a in rec["actions"]:
            out(f"    {a['kind']:<8} {a['step']:<32} {a['status']:<6} key={a['key']}")
    out(f"  {len(s.decisions.list_for_run(rid))} append-only revisions stored for this run")

    violations = check_all(w.observe())
    out(f"\nInvariants (Track F): {'all hold' if not violations else violations}")
    out(f"Done: run {rid} -> {s.gateway.get_run(rid).status}")
    return 0 if not violations and s.gateway.get_run(rid).status == "DONE" else 1


if __name__ == "__main__":
    sys.exit(main())
