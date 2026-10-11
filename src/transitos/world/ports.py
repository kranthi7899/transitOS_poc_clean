"""How the world engine reaches the products (Phase 10, Step 3.2).

`Ports` is the seam: `MockPorts` drives the real SIMULATED products through their existing `/sim` and public
routes (no mock was changed for Step 3: those routes are the hook points); `RecordingPorts` is an in-memory
double for unit tests. Query methods never appear in `RecordingPorts.calls`; only actions do.
"""
from __future__ import annotations

from typing import Any, Protocol

from transitos.mocks.clock import SimClock


class Ports(Protocol):
    def telematics_fault(self, vehicle: str, params: dict) -> None: ...
    def telematics_clear(self, vehicle: str, params: dict) -> None: ...
    def telematics_dvir(self, vehicle: str, params: dict) -> None: ...
    def scheduling_status(self, vehicle: str) -> str: ...
    def scheduling_out_of_service(self, vehicle: str) -> None: ...
    def maintenance_open_ids(self, vehicle: str) -> list[str]: ...
    def maintenance_open(self, vehicle: str, description: str) -> str | None: ...
    def maintenance_close(self, vehicle: str) -> list[str]: ...
    def workforce_callout(self, operator: str, params: dict) -> None: ...


class RecordingPorts:
    """In-memory Ports. `status` and `open_records` can be preset to model a product that already moved."""

    def __init__(self, clock: SimClock | None = None) -> None:
        self.clock = clock
        self.calls: list[tuple[Any, ...]] = []
        self.status: dict[str, str] = {}
        self.open_records: dict[str, list[str]] = {}
        self._n = 0

    def telematics_fault(self, vehicle, params):
        self.calls.append(("telematics_fault", vehicle, params))

    def telematics_clear(self, vehicle, params):
        self.calls.append(("telematics_clear", vehicle, params))

    def telematics_dvir(self, vehicle, params):
        self.calls.append(("telematics_dvir", vehicle, params))

    def scheduling_status(self, vehicle):
        return self.status.get(vehicle, "in_service")

    def scheduling_out_of_service(self, vehicle):
        self.calls.append(("scheduling_out_of_service", vehicle))
        self.status[vehicle] = "out_of_service"

    def maintenance_open_ids(self, vehicle):
        return list(self.open_records.get(vehicle, []))

    def maintenance_open(self, vehicle, description):
        self._n += 1
        wid = f"rec-{self._n}"
        self.calls.append(("maintenance_open", vehicle, description))
        self.open_records.setdefault(vehicle, []).append(wid)
        return wid

    def maintenance_close(self, vehicle):
        closed = self.open_records.pop(vehicle, [])
        self.calls.append(("maintenance_close", vehicle, tuple(closed)))
        return closed

    def workforce_callout(self, operator, params):
        self.calls.append(("workforce_callout", operator, params))


class MockPorts:
    """Drives the five SIMULATED products (a `transitos.integration.system.Mocks`, or anything with `.clients`).

    `fleet` is the mocks' own `Fleet` (mocks.seed.build_fleet): it maps canonical ids (veh-3, op-1) to each
    product's native id. An id the fleet does not know raises KeyError: a beat that cannot be shown to a product
    is a script error, never silently dropped."""

    ACTOR = "dispatcher-sim"

    def __init__(self, mocks: Any, agency: str, fleet: Any) -> None:
        self.mocks, self.agency = mocks, agency
        self.h = {"X-Agency": agency}
        self._veh = {v["canonical"]: v for v in fleet.vehicles}
        self._op = {o["canonical"]: o["native"] for o in fleet.operators}

    def _v(self, vehicle: str) -> dict:
        try:
            return self._veh[vehicle]
        except KeyError:
            raise KeyError(f"vehicle {vehicle!r} is not in the {self.agency} fleet") from None

    def _post(self, product: str, path: str, body: dict | None = None):
        r = self.mocks.clients[product].post(path, json=body or {}, headers=self.h)
        r.raise_for_status()
        return r.json()

    def _get(self, product: str, path: str):
        r = self.mocks.clients[product].get(path, headers=self.h)
        r.raise_for_status()
        return r.json()

    # telematics
    def telematics_fault(self, vehicle, params):
        self._post("telematics", "/sim/fault", {"vehicle": self._v(vehicle)["tele"], "spn": params["spn"],
                                                "fmi": params["fmi"], "lamps": params.get("lamps") or {}})

    def telematics_clear(self, vehicle, params):
        self._post("telematics", "/sim/fault/clear", {"vehicle": self._v(vehicle)["tele"], "spn": params["spn"],
                                                      "fmi": params["fmi"]})

    def telematics_dvir(self, vehicle, params):
        self._post("telematics", "/dvir", {"vehicle": self._v(vehicle)["tele"],
                                           "defect_text": params.get("defect_text", ""),
                                           "reported_by_role": "operator"})

    # scheduling
    def scheduling_status(self, vehicle):
        return self._get("scheduling", f"/vehicles/{self._v(vehicle)['sched']}")["status"]

    def scheduling_out_of_service(self, vehicle):
        self._post("scheduling", "/sim/vehicle-status", {"vehicle": self._v(vehicle)["sched"],
                                                         "status": "out_of_service", "changed_by": self.ACTOR})

    # maintenance
    def maintenance_open_ids(self, vehicle):
        asset = self._v(vehicle)["maint"]
        return [w["id"] for w in self._get("maintenance", f"/workorders?asset={asset}") if w["status"] != "closed"]

    def maintenance_open(self, vehicle, description):
        wo = self._post("maintenance", "/workorders", {"asset": self._v(vehicle)["maint"],
                                                       "description": description, "opened_by": "human"})
        return wo["id"]

    def maintenance_close(self, vehicle):
        ids = self.maintenance_open_ids(vehicle)
        for wid in ids:
            self._post("maintenance", f"/sim/workorders/{wid}/close")
        return ids

    # workforce
    def workforce_callout(self, operator, params):
        native = self._op.get(operator)
        if native is None:
            raise KeyError(f"operator {operator!r} is not in the {self.agency} fleet")
        cur = self._get("workforce", f"/operators/{native}")
        self._post("workforce", "/sim/operator", {"operator": native, "available_to": cur["available_from"],
                                                  "status": "unavailable"})
