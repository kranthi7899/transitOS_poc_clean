"""CloudEvents 1.0 structured-JSON envelope (design §2.1). Consumers ignore unknown fields."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

EventType = Literal[
    "VehicleFaultDetected",
    "PMOverdue",
    "DefectReported",
    "WorkOrderOpened",
    "VehicleStatusChanged",
    "WorkOrderClosed",
    "OperatorAvailabilityChanged",
]

START_TYPES = {"VehicleFaultDetected", "PMOverdue", "DefectReported"}
UPDATE_TYPES = {"WorkOrderOpened", "VehicleStatusChanged", "OperatorAvailabilityChanged"}
RESUME_TYPES = {"WorkOrderClosed"}

TOPIC_RAW = "events.raw"
TOPIC_RESOLVED = "events.resolved"
TOPIC_FAILED = "events.failed"


class CloudEvent(BaseModel):
    model_config = ConfigDict(extra="allow")  # unknown fields tolerated

    specversion: Literal["1.0"] = "1.0"
    id: str
    source: str = Field(description="adapter/<product>/<agency_id>")
    type: EventType
    time: datetime
    datacontenttype: Literal["application/json"] = "application/json"
    agencyid: str
    schemaversion: int = 1
    data: dict[str, Any]
