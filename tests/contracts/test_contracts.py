from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from transitos.contracts import ids
from transitos.contracts.envelope import (
    RESUME_TYPES,
    START_TYPES,
    UPDATE_TYPES,
    CloudEvent,
)
from transitos.contracts.events import DATA_MODELS, PUBLISHER, VehicleFaultDetectedData
from transitos.contracts.objects import Plan, RunState, Step
from transitos.contracts.results import Result
from transitos.contracts.tools import COMPENSATION, SAGA_ORDER

NOW = datetime(2026, 10, 10, tzinfo=UTC)


def test_every_event_type_has_model_publisher_and_role():
    types = set(DATA_MODELS)
    assert types == set(PUBLISHER)
    assert types == START_TYPES | UPDATE_TYPES | RESUME_TYPES
    assert not (START_TYPES & UPDATE_TYPES or START_TYPES & RESUME_TYPES)


def test_envelope_tolerates_unknown_fields_and_rejects_bad_type():
    e = CloudEvent(id="x", source="adapter/telematics/a1", type="PMOverdue", time=NOW,
                   agencyid="a1", data={}, futurefield=1)
    assert e.model_dump()["futurefield"] == 1
    with pytest.raises(ValidationError):
        CloudEvent(id="x", source="s", type="Nope", time=NOW, agencyid="a", data={})


def test_event_ids_are_deterministic():
    a = ids.pm_id("a1", "1106", "PM-A", 12000)
    assert a == ids.pm_id("a1", "1106", "PM-A", 12000) == "pm:a1:1106:PM-A:12000"
    assert ids.step_key("r", "create_work_order", 2) == "r:create_work_order:2"


def test_fault_data_defaults_and_extras():
    d = VehicleFaultDetectedData(native_vehicle_id="T-77", spn=110, fmi=0, newfield="ok")
    assert d.lamps.stop is False and d.source == "sensor"


def test_saga_order_irreversible_last_and_all_writes_have_compensation_entry():
    assert SAGA_ORDER[-1] == "publish_rider_alert"
    assert COMPENSATION["publish_rider_alert"] is None
    assert all(a in COMPENSATION for a in SAGA_ORDER)


def test_plan_and_runstate_roundtrip():
    plan = Plan(decision="pull_now", steps=[Step(action="create_work_order", idempotency_key="k")])
    rs = RunState(run_id="r", agency="a1", vehicle_id="V1", trigger_event={}, plan=plan)
    assert RunState.model_validate_json(rs.model_dump_json()).plan.steps[0].status == "pending"


def test_result_semantics():
    assert Result[int].success(1).ok
    assert not Result[int].unavailable("scheduling").ok


def test_every_compensation_is_a_valid_step_action():
    for action, comp in COMPENSATION.items():
        Step(action=action, idempotency_key="k", compensation=comp)


def test_resolved_event_shape_is_refs_and_canonicalid():
    from transitos.contracts.events import (
        VehicleRefs,
        caused_by_us,
        resolved_operator_id,
        resolved_vehicle_id,
    )
    data = {"native_vehicle_id": "T-77", "spn": 1, "fmi": 1,
            "refs": VehicleRefs(canonical="veh-1", telematics="T-77").model_dump()}
    e = CloudEvent(id="x", source="adapter/telematics/a1", type="VehicleFaultDetected", time=NOW,
                   agencyid="a1", data=data, canonicalid="veh-1")
    assert resolved_vehicle_id(e.data) == "veh-1" and e.canonicalid == "veh-1"
    assert VehicleFaultDetectedData.model_validate(e.data).refs.canonical == "veh-1"
    assert resolved_vehicle_id({"native_vehicle_id": "T-77"}) is None   # raw event
    assert resolved_operator_id({"operator_canonical": "op-3"}) == "op-3"
    assert caused_by_us({"changed_by": "transitos:r:set_vehicle_unavailable:1"})
    assert caused_by_us({"opened_by": "system"}) and not caused_by_us({"changed_by": "dispatcher"})


def test_blocked_by_policy_helper():
    from transitos.contracts.results import HARD_LIMIT, KILL_SWITCH
    r = Result[int].blocked(KILL_SWITCH, "agency off")
    assert r.status == "unavailable" and r.product == "policy" and r.is_blocked
    assert r.note == "kill_switch: agency off" and r.blocked_reason == KILL_SWITCH
    assert Result[int].blocked(HARD_LIMIT, "min_spares").blocked_reason == HARD_LIMIT
    assert not Result[int].unavailable("scheduling", "down").is_blocked
    with pytest.raises(ValueError):
        Result[int].blocked("nope")


def test_compensation_key_roundtrip():
    run = "a1:veh-1:fault:a1:T-77:1:1:t"
    k = ids.compensation_key(run, "reassign_block:B1", 2)
    assert k == f"{run}:comp:reassign_block:B1:2"
    assert ids.compensated_key(run, k) == ids.step_key(run, "reassign_block:B1", 2)
    assert ids.compensated_key(run, ids.step_key(run, "reassign_block:B1", 2)) is None


def test_write_tools_protocol_has_compensation_keyword():
    import inspect

    from transitos.contracts.tools import WriteTools
    for name in ("reassign_block", "assign_operator"):
        p = inspect.signature(getattr(WriteTools, name)).parameters["compensation"]
        assert p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is False


def test_committed_json_schemas_match_models(tmp_path):
    """contracts-schema/ must be regenerated (`make schemas`) whenever a contract model changes."""
    from pathlib import Path

    from transitos.contracts import export
    export.main(str(tmp_path))
    committed = Path(__file__).resolve().parents[2] / "contracts-schema"
    for f in tmp_path.glob("*.json"):
        assert (committed / f.name).read_text() == f.read_text(), f"{f.name} stale: run `make schemas`"
