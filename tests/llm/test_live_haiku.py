"""OPT-IN live check against the real Claude API (Haiku only). Skipped unless you ask for it:

    TRANSITOS_LIVE_LLM=1 ANTHROPIC_API_KEY=... make llm-smoke      (or put the key in the git-ignored .env.local)

A handful of small calls, a few thousandths of a dollar in total; the spend cap still applies."""
import os

import pytest

pytestmark = [pytest.mark.llm_live,
              pytest.mark.skipif(os.environ.get("TRANSITOS_LIVE_LLM") != "1", reason="set TRANSITOS_LIVE_LLM=1 to spend")]


def _key_available() -> bool:
    from transitos.workflow.reasoning_anthropic import load_local_env
    load_local_env(".env.local")
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


@pytest.fixture
def live_env(monkeypatch, tmp_path):
    if not _key_available():
        pytest.skip("no ANTHROPIC_API_KEY (environment or .env.local)")
    monkeypatch.setenv("TRANSITOS_REASONER", "anthropic")
    monkeypatch.setenv("TRANSITOS_LLM_SPEND_FILE", str(tmp_path / "spend.json"))
    monkeypatch.setenv("TRANSITOS_LLM_CAP_USD", "0.25")           # this test can never spend more than a quarter
    return tmp_path / "spend.json"


def test_one_real_call_returns_a_proposal_the_validator_accepts(live_env):
    from test_reasoning_anthropic import small_facts

    from transitos.workflow.reasoning import reasoner_from_env, validate_output
    facts = small_facts()
    facts.pop("clearance"), facts.pop("clearance_policy")
    r = reasoner_from_env()
    assert r.name == "claude-haiku-5-5"
    raw = r.propose(facts)
    out, errs = validate_output(raw, facts)
    assert out is not None and errs == [], (raw, errs)
    assert r.budget.total_usd < 0.01                                # one small call costs a fraction of a cent


def test_three_scenarios_pass_end_to_end_with_haiku_as_the_reasoner(live_env):
    from transitos.integration.world import IntegrationWorld
    from transitos.verification.runner import run_scenario
    from transitos.verification.scenarios import BY_ID
    for sid in ("severe_fault_with_spare", "operator_swap_needed", "larger_class_fallback"):
        res = run_scenario(IntegrationWorld(), BY_ID[sid])
        assert res.passed, (sid, res.mismatches)
