"""Event `data` payloads per type (design §2.2). Minimal fields, no rider PII.
Schemas only grow: new optional fields are fine; a breaking change means a new event type."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Data(BaseModel):
    model_config = ConfigDict(extra="allow")


class VehicleRefs(BaseModel):
    """Added by the crosswalk to every resolved event."""
    canonical: str
    telematics: str | None = None
    maintenance: str | None = None
    scheduling: str | None = None


class Lamps(BaseModel):
    stop: bool = False
    warn: bool = False
    protect: bool = False


class VehicleFaultDetectedData(_Data):
    native_vehicle_id: str
    spn: int
    fmi: int
    lamps: Lamps = Field(default_factory=Lamps)
    occurrence_count: int = 1
    source: Literal["sensor", "human"] = "sensor"


class PMOverdueData(_Data):
    native_asset_id: str
    service_code: str
    due_mi: int
    meter_mi: int


class DefectReportedData(_Data):
    native_vehicle_id: str
    defect_text: str
    reported_by_role: str = "operator"


class WorkOrderOpenedData(_Data):
    native_asset_id: str
    wo_no: str
    opened_by: Literal["system", "human"] = "human"


class VehicleStatusChangedData(_Data):
    native_vehicle_id: str
    status: Literal["ready", "in_service", "out_of_service", "reserved"]
    changed_by: str = ""


class WorkOrderClosedData(_Data):
    native_asset_id: str
    wo_no: str


class OperatorAvailabilityChangedData(_Data):
    native_operator_id: str
    status: str


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
