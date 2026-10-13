"""One opt-in paid scenario, using the persistent ledger. Never runs in make verify."""
import os

import pytest

from transitos.integration.world import IntegrationWorld
from transitos.verification.runner import run_scenario
from transitos.verification.scenarios import BY_ID
from transitos.workflow.reasoning import reasoner_from_env
from transitos.workflow.reasoning_openai import load_openai_key

pytestmark = [pytest.mark.openai_live, pytest.mark.skipif(
    os.environ.get("TRANSITOS_LIVE_OPENAI") != "1", reason="set TRANSITOS_LIVE_OPENAI=1 to spend")]


def test_one_live_openai_scenario_uses_validated_model_answer(monkeypatch):
    load_openai_key()
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.fail("Configure OPENAI_API_KEY locally before running the paid rehearsal")
    monkeypatch.setenv("TRANSITOS_REASONER", "openai")
    reasoner = reasoner_from_env()
    world = IntegrationWorld(reasoner=reasoner, bus="sqlite", db="sqlite")
    try:
        result = run_scenario(world, BY_ID["operator_swap_needed"])
        assert result.passed, result.mismatches
        run = world.observe().runs[0]
        assert not run.plan.fallback_used, "Live proposal used fallback; check API access and the ledger"
        assert run.status == "DONE"
        assert reasoner.budget.status()["spent_usd"] <= 0.5
    finally:
        if world.system:
            world.system.close()
        world._drop_data_dir()
