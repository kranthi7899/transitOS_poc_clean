import pytest

from transitos.integration.console import DemoSession
from transitos.verification.demo_evals import CASES
from transitos.workflow.memory import IncidentMemory
from transitos.workflow.reasoning import StubReasoner


@pytest.mark.parametrize("ident,title,evaluate", CASES, ids=[case[0] for case in CASES])
def test_behavioral_eval(ident, title, evaluate, tmp_path):
    session = DemoSession(StubReasoner(), IncidentMemory(tmp_path / "memory.sqlite"))
    try:
        assert evaluate(session, tmp_path)
    finally:
        session.close()
