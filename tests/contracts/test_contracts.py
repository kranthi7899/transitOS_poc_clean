from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from transitos.contracts import ids
from transitos.contracts.envelope import CloudEvent, START_TYPES, UPDATE_TYPES, RESUME_TYPES
from transitos.contracts.events import DATA_MODELS, PUBLISHER, VehicleFaultDetectedData
from transitos.contracts.objects import Plan, RunState, Step
from transitos.contracts.results import Result
from transitos.contracts.tools import COMPENSATION, SAGA_ORDER

NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)


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
