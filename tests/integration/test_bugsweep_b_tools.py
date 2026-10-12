"""Bug sweep B, tools side, through the REAL adapters and the REAL mock products.

F9-R4 (A153): an unreadable MAINTENANCE part of `get_vehicle_history` (agency timezone unknown) no longer makes the
whole history unavailable attributed to scheduling: the scheduling history is returned, status partial, product
maintenance, note `maintenance_history_missing: timezone_unknown`.

F8-R4 (A154): the request is read OPEN, the foreman converts it before the cancel call, the product answers 409.
The adapter re-reads the request and, when it now sits on the work order it created, cancels THAT work order through
the work-order cancel path; any other 409 still surfaces. Expected rows are written from the ruling text."""
from __future__ import annotations

from typing import Any

import httpx
import pytest

from transitos.tools.idem_store import IdemStore
from transitos.tools.policy import KillSwitchStore
from transitos.tools.registry import Registry

from .conftest import AG, H
from .test_step4_adapters import LOCAL, PENDING, crosswalk, mlocal, prs, tools, wos

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ------------------------------------------------------------------ F9-R4
def _history_fixture(mocks) -> None:
    """veh-1 = scheduling BUS-44 / maintenance asset 1106: one scheduling status change, one work order."""
    mocks.c("scheduling").post("/sim/vehicle-status", json={"vehicle": "BUS-44", "status": "out_of_service",
                                                            "changed_by": "dispatcher"}, headers=H)
    mocks.c("maintenance").post("/workorders", json={"asset": "1106", "opened_by": "human"}, headers=H)


@pytest.mark.parametrize("tz", [None, "", "Mars/Olympus_Mons"])
def test_history_with_an_unknown_maintenance_timezone_is_partial_and_keeps_the_scheduling_part(mocks, tz):
    mlocal(mocks)                                         # maintenance sends zone-less local time
    ts, _ = tools(mocks, {"local_time": True, "timezone": tz})
    _history_fixture(mocks)
    r = ts.get_vehicle_history(AG, "veh-1")
    assert (r.status, r.product, r.note) == ("partial", "maintenance", "maintenance_history_missing: timezone_unknown")
    kinds = [h["kind"] for h in r.value]
    assert kinds == ["status_change"]                     # the scheduling part is there, no guessed work order
    assert r.value[0]["status"] == "out_of_service" and r.value[0]["vehicle_id"] == "veh-1"


def test_history_with_a_malformed_maintenance_time_is_attributed_to_maintenance(mocks):
    """Same attribution for any unreadable maintenance part (own decision, A153): the product sends a zoned time
    while the agency says local time (mixed formats)."""
    ts, _ = tools(mocks, LOCAL)
    _history_fixture(mocks)
    r = ts.get_vehicle_history(AG, "veh-1")
    assert (r.status, r.product) == ("partial", "maintenance")
    assert (r.note or "").startswith("maintenance_history_missing: malformed")
    assert [h["kind"] for h in r.value] == ["status_change"]


def test_history_with_a_known_timezone_is_unchanged(mocks):
    mlocal(mocks)
    ts, _ = tools(mocks, LOCAL)
    _history_fixture(mocks)
    r = ts.get_vehicle_history(AG, "veh-1")
    assert r.status == "ok" and sorted(h["kind"] for h in r.value) == ["status_change", "work_order"]


def test_history_when_scheduling_itself_fails_is_still_unavailable_scheduling(mocks):
    mlocal(mocks)
    ts, _ = tools(mocks, {"local_time": True, "timezone": None})
    mocks.chaos["scheduling"].set("timeout")
    r = ts.get_vehicle_history(AG, "veh-1")
    assert (r.status, r.product) == ("unavailable", "scheduling")


# ------------------------------------------------------------------ F8-R4
class RaceClient:
    """The maintenance TestClient with the foreman's conversion injected deterministically: the moment the adapter
    sends POST /pending-repairs/{id}/cancel, the foreman converts that request first (real /sim route)."""

    def __init__(self, inner: Any, convert: bool = True) -> None:
        self.inner, self.convert = inner, convert
        self.raced: list[str] = []

    def request(self, method: str, path: str, **kw: Any):
        if method == "POST" and path.startswith("/pending-repairs/") and path.endswith("/cancel") and self.convert:
            pr_id = path.split("/")[2]
            self.inner.post(f"/sim/pending-repairs/{pr_id}/convert", headers=H)
            self.raced.append(pr_id)
        return self.inner.request(method, path, **kw)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


def _tools_with(mocks, settings: dict, maintenance: Any) -> tuple[Any, KillSwitchStore]:
    ks = KillSwitchStore()
    clients = {n: mocks.c(n) for n in ("scheduling", "workforce", "alerts")}
    reg = Registry(clients={**clients, "maintenance": maintenance}, crosswalk=crosswalk(), idem=IdemStore(),
                   killswitch=ks, sleep=lambda s: None, settings_patch={AG: dict(settings)})
    return reg.toolset(AG), ks


def _race_tools(mocks, settings: dict):
    race = RaceClient(mocks.c("maintenance"))
    ts, ks = _tools_with(mocks, settings, race)
    return ts, race, ks


def test_race_open_request_converted_before_the_cancel_cancels_the_work_order_it_became(mocks):
    ts, race, _ks = _race_tools(mocks, PENDING)
    a = ts.create_work_order(AG, "veh-1", "coolant", "run:create_work_order:1").value
    assert prs(mocks)[0]["status"] == "OPEN" and wos(mocks) == []
    r = ts.cancel_work_order(AG, a.id, "run:cancel_work_order:1")
    assert race.raced == [a.id]                       # the race really happened between the read and the cancel
    assert r.status == "ok", r.note
    assert r.value.id == a.id and r.value.status == "closed"
    (wo,) = wos(mocks)
    assert wo["from_pending_repair"] == a.id and wo["status"] == "CLOSED" and wo["cancelled"] is True
    assert prs(mocks)[0]["status"] == "CONVERTED"
    again = ts.cancel_work_order(AG, a.id, "run:cancel_work_order:1")       # idempotent replay: same result
    assert again == r and len(wos(mocks)) == 1


def test_race_into_a_work_order_a_person_opened_withdraws_the_request_and_leaves_theirs(mocks):
    """No 409 here (the request joined someone else's work order): the request is withdrawn as before."""
    ts, race, _ks = _race_tools(mocks, PENDING)
    a = ts.create_work_order(AG, "veh-1", "coolant", "k1").value
    human = mocks.c("maintenance").post("/workorders", json={"asset": "1106", "opened_by": "human"}, headers=H).json()
    r = ts.cancel_work_order(AG, a.id, "c1")
    assert race.raced == [a.id] and r.status == "ok"
    assert mocks.c("maintenance").get(f"/workorders/{human['id']}", headers=H).json()["status"] == "open"
    assert mocks.c("maintenance").get(f"/pending-repairs/{a.id}", headers=H).json()["status"] == "CANCELLED"


class Refuse409(RaceClient):
    """Every cancel of a pending repair answers 409 for some other reason; the request stays OPEN."""

    def request(self, method: str, path: str, **kw: Any):
        if method == "POST" and path.startswith("/pending-repairs/") and path.endswith("/cancel"):
            return httpx.Response(409, text="refused for another reason", request=httpx.Request(method, path))
        return self.inner.request(method, path, **kw)


def test_a_409_that_is_not_the_conversion_race_still_surfaces(mocks):
    ts, _ = _tools_with(mocks, PENDING, Refuse409(mocks.c("maintenance"), convert=False))
    a = ts.create_work_order(AG, "veh-1", "coolant", "k1").value
    r = ts.cancel_work_order(AG, a.id, "c1")
    assert (r.status, r.product) == ("unavailable", "maintenance") and "409" in (r.note or "")
    assert prs(mocks)[0]["status"] == "OPEN" and wos(mocks) == []


def test_race_cancel_is_blocked_by_the_kill_switch_like_any_write(mocks):
    ts, race, ks = _race_tools(mocks, PENDING)
    a = ts.create_work_order(AG, "veh-1", "coolant", "k1").value
    ks.set(AG, True)
    r = ts.cancel_work_order(AG, a.id, "c1")
    assert r.is_blocked and r.blocked_reason == "kill_switch"
    assert race.raced == [] and prs(mocks)[0]["status"] == "OPEN" and wos(mocks) == []


def test_race_on_a_request_a_person_raised_is_out_of_scope(mocks):
    """Hard limit unchanged: a human-raised request is never cancelled, raced or not."""
    ts, race, _ = _race_tools(mocks, PENDING)
    pr = mocks.c("maintenance").post("/pending-repairs", json={"asset": "1106", "requested_by": "human"},
                                     headers=H).json()
    r = ts.cancel_work_order(AG, pr["id"], "c1")
    assert r.is_blocked and r.blocked_reason == "hard_limit"
    assert race.raced == [] and wos(mocks) == []
