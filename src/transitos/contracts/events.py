"""Event `data` payloads per type (design §2.2). Minimal fields, no rider PII.
Schemas only grow: new optional fields are fine; a breaking change means a new event type."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Data(BaseModel):
    model_config = ConfigDict(extra="allow")


class VehicleRefs(BaseModel):
    """Added by the crosswalk to every resolved vehicle event as `data["refs"]` (the ONE canonical shape;
    the envelope also carries `canonicalid`). Absent on events.raw."""
    canonical: str
    telematics: str | None = None
    maintenance: str | None = None
    scheduling: str | None = None


class _VehicleData(_Data):
    refs: VehicleRefs | None = None  # set by the crosswalk on events.resolved
    # v1.2 (B-1): identity evidence as the product reported it, for the resolver
    native_vin: str | None = None
    native_plate: str | None = None
    native_name: str | None = None


class Lamps(BaseModel):
    stop: bool = False
    warn: bool = False
    protect: bool = False


class VehicleFaultDetectedData(_VehicleData):
    native_vehicle_id: str
    spn: int
    fmi: int
    lamps: Lamps = Field(default_factory=Lamps)
    occurrence_count: int = 1
    source: Literal["sensor", "human"] = "sensor"


class PMOverdueData(_VehicleData):
    native_asset_id: str
    service_code: str
    due_mi: int
    meter_mi: int


class DefectReportedData(_VehicleData):
    native_vehicle_id: str
    defect_text: str
    reported_by_role: str = "operator"


class WorkOrderOpenedData(_VehicleData):
    native_asset_id: str
    wo_no: str
    opened_by: Literal["system", "human"] = "human"


class VehicleStatusChangedData(_VehicleData):
    native_vehicle_id: str
    status: Literal["ready", "in_service", "out_of_service", "reserved"]
    changed_by: str = ""


class WorkOrderClosedData(_VehicleData):
    native_asset_id: str
    wo_no: str


class OperatorAvailabilityChangedData(_Data):
    native_operator_id: str
    status: str
    operator_canonical: str | None = None  # set by the crosswalk on events.resolved


DATA_MODELS: dict[str, type[BaseModel]] = {
    "VehicleFaultDetected": VehicleFaultDetectedData,
    "PMOverdue": PMOverdueData,
    "DefectReported": DefectReportedData,
    "WorkOrderOpened": WorkOrderOpenedData,
    "VehicleStatusChanged": VehicleStatusChangedData,
    "WorkOrderClosed": WorkOrderClosedData,
    "OperatorAvailabilityChanged": OperatorAvailabilityChangedData,
}

# Which product publishes which event (design §2.2)
PUBLISHER: dict[str, str] = {
    "VehicleFaultDetected": "telematics",
    "DefectReported": "telematics",
    "PMOverdue": "maintenance",
    "WorkOrderOpened": "maintenance",
    "WorkOrderClosed": "maintenance",
    "VehicleStatusChanged": "scheduling",
    "OperatorAvailabilityChanged": "workforce",
}


# ---- resolved-event accessors (the single shape every consumer uses) -----------------------------
REFS_KEY = "refs"                      # data["refs"] = VehicleRefs dump (vehicle events)
OPERATOR_KEY = "operator_canonical"    # data["operator_canonical"] (operator events)


def resolved_vehicle_id(data: dict) -> str | None:
    """Canonical vehicle id of a resolved event's `data`, or None (raw / operator event)."""
    refs = data.get(REFS_KEY)
    return refs.get("canonical") if isinstance(refs, dict) else None


def resolved_operator_id(data: dict) -> str | None:
    return data.get(OPERATOR_KEY)


# ---- self-inflicted updates -------------------------------------------------------------------------
SYSTEM_ACTOR = "transitos"  # writes by TransitOS: scheduling changed_by "transitos:<idempotency key>",
                            # maintenance opened_by "system"


def caused_by_us(data: dict) -> bool:
    """True if an update event was caused by one of our own writes (must not invalidate our plan)."""
    return data.get("opened_by") == "system" or str(data.get("changed_by", "")).startswith(SYSTEM_ACTOR)
