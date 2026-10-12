"""Bug sweep A2, H3 (A161) through the REAL panel and System, written from the ruling before the code: a human pick in
the panel derives the mapping's entity from the PRODUCT on the server (workforce -> operator; telematics, scheduling
-> vehicle; maintenance -> asset), never from the page's hidden field. Before the fix a pick for a parked workforce id
was stored as entity 'vehicle', so `native_of(..., operator=True)` found nothing and assign_operator was unavailable."""
from __future__ import annotations

import pytest

from transitos.contracts.envelope import TOPIC_RAW, CloudEvent
from transitos.integration.world import IntegrationWorld
from transitos.verification.scenarios import AGENCY, BY_ID


@pytest.fixture
def world():
    w = IntegrationWorld()
    yield w
    if w.system is not None:
        w.system.close()


def _park_op2(world) -> None:
    """OP-2's crosswalk row is lost; an availability event from OP-2 is parked."""
    world.apply(BY_ID["retired_spare_never_proposed"])
    s = world.system
    s.db.execute("DELETE FROM xw_entries WHERE agency=? AND product='workforce' AND native_id='OP-2'", (AGENCY,))
    s.bus.send(TOPIC_RAW, "workforce:OP-2", CloudEvent(
        id="op2-avail", source=f"adapter/workforce/{AGENCY}", type="OperatorAvailabilityChanged",
        time=s.clock.now(), agencyid=AGENCY, data={"native_operator_id": "OP-2", "status": "available"}))
    s.xw_svc.drain()
    assert any(u["native_id"] == "OP-2" for u in s.xw.unresolved(AGENCY))


def test_h3_a_ui_pick_for_a_workforce_id_is_stored_as_an_operator(world):
    _park_op2(world)
    s = world.system
    page = s.panel_client.get("/ui/unresolved").text
    assert "OP-2" in page and "name='entity'" not in page          # the page no longer carries an entity
    # the old page's form (hidden entity 'vehicle'), the human typing op-2: the server must still store an operator
    r = s.panel_client.post("/ui/crosswalk", data={"agency": AGENCY, "product": "workforce", "native_id": "OP-2",
                                                   "entity": "vehicle", "canonical_id": "op-2"})
    assert r.status_code < 400, r.text
    [row] = [x for x in s.xw.rows(AGENCY) if x.product == "workforce" and x.native_id == "OP-2"]
    assert row.entity == "operator" and row.canonical_id == "op-2"
    assert s.xw.all_ids(AGENCY, "op-2", entities=("operator",)) == {"workforce": "OP-2"}
    res = s.toolset.assign_operator(AGENCY, "blk-1", "op-2", "k-h3")
    assert res.status == "ok" and res.value is not None and res.value.operator_id == "op-2", res


def test_h3_the_json_pick_derives_the_entity_too(world):
    _park_op2(world)
    s = world.system
    r = s.panel_client.post("/crosswalk", json={"agency": AGENCY, "product": "workforce", "native_id": "OP-2",
                                                "entity": "vehicle", "canonical_id": "op-2"})
    assert r.status_code == 200, r.text
    [row] = [x for x in s.xw.rows(AGENCY) if x.product == "workforce" and x.native_id == "OP-2"]
    assert row.entity == "operator"


def test_h3_an_unknown_product_is_refused(world):
    world.apply(BY_ID["retired_spare_never_proposed"])
    r = world.system.panel_client.post("/crosswalk", json={"agency": AGENCY, "product": "payroll", "native_id": "X",
                                                           "entity": "vehicle", "canonical_id": "veh-1"})
    assert r.status_code == 422, r.text
    assert not [x for x in world.system.xw.rows(AGENCY) if x.product == "payroll"]
