"""The people and devices that turn ground truth into what each product holds (Phase 10, Step 3.2).

Each actor owns ONE product and ONE lag. `handles(beat)` says whether the beat concerns it at all; `act` returns
None when it did its job or a reason string when a rule made it stand down (a skip is logged by the engine, it is
not an observation). Severity comes from the committed J1939 fixture's configurable rule (never ground truth).

  TelematicsActor  fault_on / fault_off / dvir  -> telematics, after telematics_s
  ShopActor        fault_on / dvir -> opens a maintenance record, return_to_service -> closes it, after
                   shop_rule_min (A91). It stands down when a record is already open for the vehicle (a rule
                   de-duplicates). Records are opened as `human` (A92: a foreman or controller keys it, so the
                   orchestrator reuses it instead of opening a second one).
  DispatcherActor  fault_on with the stop lamp -> sets scheduling out_of_service, call-out -> records the operator
                   as unavailable in workforce, after dispatcher_min. It stands down when scheduling already
                   shows the vehicle out of service.
"""
from __future__ import annotations

from datetime import timedelta

from transitos.data.loaders import load_j1939

from .lag import ProductLag
from .ports import Ports
from .script import Beat

_SEVERITY_OPENS_RECORD = ("service_soon", "pull_vehicle")


class Actor:
    product: str = ""
    kinds: tuple[str, ...] = ()

    def __init__(self, lag: ProductLag) -> None:
        self.lag = lag

    def product_of(self, beat: Beat) -> str:
        """The product whose view this beat changes (DispatcherActor serves two)."""
        return self.product

    def delay(self, beat: Beat) -> timedelta:
        return self.lag.delay(self.product_of(beat))

    def handles(self, beat: Beat) -> bool:
        return beat.kind in self.kinds

    def act(self, beat: Beat, ports: Ports) -> str | None:   # pragma: no cover - interface
        raise NotImplementedError


class TelematicsActor(Actor):
    product = "telematics"
    kinds = ("fault_on", "fault_off", "dvir")

    def handles(self, beat: Beat) -> bool:
        # a fault without an SPN is not a J1939 fault: telematics cannot show it (a driver reports it by DVIR)
        if beat.kind in ("fault_on", "fault_off"):
            return beat.params.get("spn") is not None and beat.params.get("fmi") is not None
        return beat.kind == "dvir"

    def act(self, beat: Beat, ports: Ports) -> str | None:
        {"fault_on": ports.telematics_fault, "fault_off": ports.telematics_clear,
         "dvir": ports.telematics_dvir}[beat.kind](beat.vehicle, dict(beat.params))
        return None


class ShopActor(Actor):
    product = "maintenance"
    kinds = ("fault_on", "dvir", "return_to_service")

    def __init__(self, lag: ProductLag) -> None:
        super().__init__(lag)
        self._j1939 = load_j1939()

    def handles(self, beat: Beat) -> bool:
        if beat.kind == "fault_on":
            return self._opens_record(beat)
        return beat.kind in ("dvir", "return_to_service")

    def _opens_record(self, beat: Beat) -> bool:
        p = beat.params
        if (p.get("lamps") or {}).get("stop"):
            return True
        if p.get("spn") is None or p.get("fmi") is None:
            return False
        return self._j1939.severity(int(p["spn"]), int(p["fmi"])) in _SEVERITY_OPENS_RECORD

    def act(self, beat: Beat, ports: Ports) -> str | None:
        if beat.kind == "return_to_service":
            ports.maintenance_close(beat.vehicle)
            return None
        if ports.maintenance_open_ids(beat.vehicle):
            return "a record is already open for the vehicle"
        what = beat.params.get("defect_text") or f"SPN {beat.params.get('spn')} FMI {beat.params.get('fmi')}"
        ports.maintenance_open(beat.vehicle, f"shop rule: {what}")
        return None


class DispatcherActor(Actor):
    product = "scheduling"
    kinds = ("fault_on", "callout")

    def handles(self, beat: Beat) -> bool:
        if beat.kind == "fault_on":
            return bool((beat.params.get("lamps") or {}).get("stop"))
        return beat.kind == "callout"

    def product_of(self, beat: Beat) -> str:
        return "workforce" if beat.kind == "callout" else "scheduling"

    def act(self, beat: Beat, ports: Ports) -> str | None:
        if beat.kind == "callout":
            ports.workforce_callout(beat.vehicle, dict(beat.params))
            return None
        if ports.scheduling_status(beat.vehicle) == "out_of_service":
            return "scheduling already shows the vehicle out of service"
        ports.scheduling_out_of_service(beat.vehicle)
        return None
