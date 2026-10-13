from transitos.integration.console import Command, DemoSession
from transitos.workflow.memory import IncidentMemory, MemoryReasoner
from transitos.workflow.reasoning import StubReasoner


def complete(session):
    for action in ("trigger", "approve", "repair", "approve"):
        current = session.state()
        run = next(iter(current["runs"]), {})
        session.perform(Command(action=action, generation=current["generation"], plan_version=run.get("plan_version"),
                                scope="return_to_service" if run.get("status") == "RETURNING" else "plan"))
        assert session.error is None


def test_approved_memory_survives_reset_and_new_connection_but_never_approves(tmp_path):
    path = tmp_path / "memory.sqlite"
    session = DemoSession(StubReasoner(), IncidentMemory(path))
    try:
        complete(session)
        assert len(IncidentMemory(path).recall("agency-a", "veh-1")) == 1
        session.publish()
        assert len(session.state()["memory"]) == 1  # idempotent repeat publication
        session.reset()
        assert len(session.state()["memory"]) == 1 and session.state()["writes"] == []
        session.perform(Command(action="trigger", generation=session.generation))
        assert session.state()["runs"][0]["status"] == "AWAITING_APPROVAL"
        assert session.state()["writes"] == []
        assert IncidentMemory(path).recall("different-agency", "veh-1") == []
    finally:
        session.close()


def test_rejected_incident_is_not_learned(tmp_path):
    session = DemoSession(StubReasoner(), IncidentMemory(tmp_path / "memory.sqlite"))
    try:
        session.perform(Command(action="trigger", generation=session.generation))
        session.perform(Command(action="reject", generation=session.generation, plan_version=1))
        assert session.state()["memory"] == []
    finally:
        session.close()


def test_unreadable_memory_does_not_break_reasoning(tmp_path):
    path = tmp_path / "memory.sqlite"
    path.write_text("not a sqlite file")

    class Recorder:
        name, prompt_version = "recorder", "v1"

        def propose(self, facts, attempt=0):
            assert facts["prior_incidents"] == []
            return {"reasoning": "safe current facts"}

    assert MemoryReasoner(Recorder(), IncidentMemory(path), "a").propose({"vehicle": {"id": "v"}})["reasoning"]
