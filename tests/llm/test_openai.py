import json

import httpx
import pytest

from transitos.workflow.reasoning import reasoner_from_env
from transitos.workflow.reasoning_openai import (
    DemoBudget,
    OpenAIConfigError,
    OpenAIReasoner,
    OpenAIUnavailable,
)


def reply(usage=None, text=None):
    return {"status": "completed", "usage": usage or {"input_tokens": 100, "output_tokens": 100},
            "output": [{"content": [{"type": "output_text", "text": text or json.dumps({
                "decision": "pull_now", "pull_at": None, "assignments": [], "reasoning": "Use the eligible spare.",
                "assumptions": []})}]}]}


def test_openai_structured_proposal_and_usage(tmp_path):
    seen = []

    def handle(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=reply())

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        r = OpenAIReasoner(client=client, budget=DemoBudget(tmp_path / "spend.db"))
        assert r.propose({"blocks": []})["reasoning"] == "Use the eligible spare."
    assert seen[0]["text"]["format"]["strict"] is True
    assert seen[0]["store"] is False and "tools" not in seen[0]
    assert r.budget.status()["spent_usd"] == pytest.approx(0.000075)


def test_budget_is_durable_capped_and_checked_before_call(tmp_path):
    path = tmp_path / "spend.db"
    budget = DemoBudget(path, cap=120)
    assert budget.cap == 0.5
    budget.reserve(0.5)
    with httpx.Client(transport=httpx.MockTransport(lambda req: pytest.fail("must not call"))) as client:
        r = OpenAIReasoner(client=client, budget=DemoBudget(path))
        with pytest.raises(OpenAIUnavailable, match="cap reached"):
            r.propose({"blocks": []})
    assert DemoBudget(path).status()["calls"] == 1


def test_uncertain_failure_retains_cost_without_secret_error(tmp_path):
    with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(401, json={"error": "secret"}))) as c:
        r = OpenAIReasoner(key="private-placeholder", client=c, budget=DemoBudget(tmp_path / "spend.db"))
        with pytest.raises(OpenAIUnavailable) as error:
            r.propose({"blocks": []})
    assert "secret" not in str(error.value) and "private" not in str(error.value)
    assert r.budget.status()["spent_usd"] > 0


def test_corrupt_ledger_fails_closed(tmp_path):
    path = tmp_path / "spend.db"
    path.write_text("not sqlite")
    assert DemoBudget(path).status()["available"] is False
    with pytest.raises(OpenAIUnavailable, match="ledger unavailable"):
        DemoBudget(path).reserve(0.001)


def test_factory_requires_key_and_rejects_nonfinite_cap(tmp_path):
    with pytest.raises(OpenAIConfigError, match="OPENAI_API_KEY"):
        reasoner_from_env({"TRANSITOS_REASONER": "openai"}, load_env_file=False)
    with pytest.raises(OpenAIConfigError, match="finite"):
        DemoBudget(tmp_path / "spend.db", cap=float("nan"))
    assert reasoner_from_env({"TRANSITOS_REASONER": "stub"}).name == "stub"
