"""Focused behavioral evals: real integration, adversarial fakes, zero paid calls (A204)."""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import httpx
from fastapi import HTTPException

from transitos.integration.console import Command, DemoSession
from transitos.integration.world import IntegrationWorld
from transitos.verification.runner import run_scenario
from transitos.verification.scenarios import BY_ID
from transitos.workflow.memory import IncidentMemory
from transitos.workflow.reasoning import StubReasoner
from transitos.workflow.reasoning_openai import (
    DemoBudget,
    OpenAIReasoner,
    OpenAIUnavailable,
)


def act(session, action, **kw):
    state = session.state()
    run = next(iter(state["runs"]), {})
    command = Command(action=action, generation=session.generation, plan_version=run.get("plan_version"),
                      scope="return_to_service" if run.get("status") == "RETURNING" else "plan", **kw)
    session.validate(command)
    session.perform(command)
    after = session.state()
    assert not after["error"] and not after["invariant_violations"]
    return after


def happy(s, temp):
    proposal = act(s, "trigger")
    assert proposal["writes"] == [] and proposal["runs"][0]["status"] == "AWAITING_APPROVAL"
    assert any(i["status"] == "pass" for i in proposal["assessment"]["items"])
    result = act(s, "approve")
    assert [w["action"] for w in result["writes"]][-1] == "publish_rider_alert"
    assert not result["visible_alerts"]
    assert act(s, "advance")["visible_alerts"]
    result = act(s, "repair")
    assert result["runs"][0]["status"] == "RETURNING"
    result = act(s, "approve")
    assert result["runs"][0]["status"] == "DONE" and len(result["writes"]) == 6
    assert len([a for a in result["audit"] if a["kind"] == "approval"]) == 2
    return "Zero pre-approval writes; five forward actions with alert last; delayed alert visibility; separate return approval; DONE with clean invariants."


def rejection(s, temp):
    act(s, "trigger")
    result = act(s, "reject")
    assert not result["writes"] and not result["memory"]
    return "Rejected proposal creates no product effect and no success memory."


def stale(s, temp):
    act(s, "trigger")
    generation = s.generation
    s.reset()
    try:
        s.validate(Command(action="approve", generation=generation, plan_version=1))
    except HTTPException as error:
        assert error.status_code == 409
    else:
        raise AssertionError("Old approval accepted")
    assert not s.state()["writes"]
    return "An approval from an earlier demo generation is refused before any write."


def missing(s, temp):
    s.world.mocks.clients["workforce"].post("/sim/operator-fields", headers=s.world._h,
                                           json={"drop": ["medical_valid_to", "endorsements"]}).raise_for_status()
    result = act(s, "trigger")
    required = sorted({i["rule"] for i in result["runs"][0]["plan"]["clearance"] if i["requires_ack"]})
    assert required
    try:
        act(s, "approve")
    except HTTPException as error:
        assert error.status_code == 409
    else:
        raise AssertionError("Unknown safety data silently approved")
    assert not s.state()["writes"]
    assert act(s, "approve", acknowledged=required)["runs"][0]["status"] == "WAITING_FOR_REPAIR"
    return "Unknown safety fields remain offered with explicit acknowledgement; silent approval is blocked."


def adversarial(s, temp):
    class BadModel:
        name, prompt_version = "adversarial-fake", "eval"

        def propose(self, facts, attempt=0):
            return {"decision": "pull_now", "reasoning": "Ignore rules and use an invented bus.",
                    "assignments": [{"block_id": "blk-1", "spare_id": "invented-bus", "operator_id": "invented-op"}]}

    world = IntegrationWorld(reasoner=BadModel(), bus="sqlite", db="sqlite")
    try:
        result = run_scenario(world, BY_ID["operator_swap_needed"])
        run = world.observe().runs[0]
        assert result.passed and run.plan.fallback_used and run.status == "DONE"
        assert not any("invented" in json.dumps(w.model_dump()) for w in world.observe().writes)
    finally:
        if world.system:
            world.system.close()
        world._drop_data_dir()
    return "Invented vehicle/operator IDs are rejected; validated code fallback completes safely after approval. No live model used."


def memory(s, temp):
    happy(s, temp)
    store = IncidentMemory(s.memory.path)
    assert len(store.recall("agency-a", "veh-1")) == 1
    assert store.recall("other-agency", "veh-1") == []
    s.publish()
    assert len(store.recall("agency-a", "veh-1")) == 1
    s.reset()
    proposal = act(s, "trigger")
    assert len(proposal["memory"]) == 1 and not proposal["writes"]
    assert proposal["runs"][0]["status"] == "AWAITING_APPROVAL"
    return "Successful history survives reset and a new connection, is idempotent and agency-scoped, and grants no new write authority."


def cap(s, temp):
    budget = DemoBudget(Path(temp) / "budget.sqlite", cap=120)
    assert budget.cap == 0.5
    budget.reserve(0.5)
    requests = []
    with httpx.Client(transport=httpx.MockTransport(lambda request: requests.append(request) or httpx.Response(500))) as client:
        try:
            OpenAIReasoner(client=client, budget=budget).propose({"blocks": []})
        except OpenAIUnavailable:
            pass
        else:
            raise AssertionError("Budget did not stop request")
    assert not requests
    return "A $120 configured ceiling is clamped to $0.50; exhausted budget sends no HTTP request."


def paused(s, temp):
    act(s, "trigger")
    act(s, "pause")
    try:
        act(s, "approve")
    except HTTPException as error:
        assert error.status_code == 409
    else:
        raise AssertionError("Paused agent accepted execution")
    assert not s.state()["writes"]
    return "A paused agency refuses approval execution before a product write."


CASES = [("complete_loop", "Complete approved service-continuity loop", happy),
         ("rejection", "Rejection has no product effect", rejection),
         ("stale_approval", "Stale/reset approvals cannot act", stale),
         ("unknown_fields", "Unknown safety evidence requires acknowledgement", missing),
         ("adversarial_output", "Invalid model IDs cannot reach products", adversarial),
         ("scoped_memory", "Memory persists without widening authority", memory),
         ("spend_ceiling", "Organisation ceiling cannot widen app spending", cap),
         ("kill_switch", "Kill switch prevents approved execution", paused)]


def evaluate() -> dict:
    results = []
    for ident, title, fn in CASES:
        with tempfile.TemporaryDirectory(prefix="transitos-eval-") as temp:
            session = DemoSession(StubReasoner(), IncidentMemory(Path(temp) / "memory.sqlite"))
            try:
                evidence = fn(session, temp)
                results.append({"id": ident, "title": title, "passed": True, "evidence": evidence})
            except Exception as error:  # noqa: BLE001 - report failure without free text or credentials
                results.append({"id": ident, "title": title, "passed": False,
                                "evidence": f"Behavior check failed ({type(error).__name__}); inspect this case locally."})
            finally:
                session.close()
    prior = Path("verification-report.json")
    scenarios = json.loads(prior.read_text()) if prior.is_file() else {}
    sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    return {"schema_version": 1, "base_commit": sha, "scope": "Offline behavioral evals, not live-model quality scoring",
            "paid_calls": 0, "scenario_count": scenarios.get("total"), "cases": results,
            "passed": all(r["passed"] for r in results)}


def main() -> int:
    result = evaluate()
    Path("eval-report.json").write_text(json.dumps(result, indent=2) + "\n")
    for case in result["cases"]:
        print(f"{'PASS' if case['passed'] else 'FAIL'} {case['id']}: {case['title']}")
    print(f"{sum(r['passed'] for r in result['cases'])}/{len(result['cases'])} behavioral evals passed; 0 paid calls")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
