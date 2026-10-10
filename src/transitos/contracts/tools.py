"""Tool contracts (design §4, §7.4). The workflow calls these; the per-agency registry routes them to adapters.
Reads return Result[...]; writes take an idempotency key and return Result[...]."""
from __future__ import annotations

from datetime import datetime
from typing import Protocol

from .objects import Block, Needs, Operator, Vehicle, WorkOrder
from .results import Result


class ReadTools(Protocol):
    def get_vehicle(self, agency: str, vehicle_id: str) -> Result[Vehicle]: ...
    def get_vehicle_history(self, agency: str, vehicle_id: str) -> Result[list[dict]]: ...
    def list_affected_blocks(self, agency: str, vehicle_id: str, start: datetime, end: datetime) -> Result[list[Block]]: ...
    def list_ready_vehicles(self, agency: str, start: datetime, end: datetime, needs: Needs) -> Result[list[Vehicle]]: ...
    def list_eligible_operators(self, agency: str, block: Block) -> Result[list[Operator]]: ...
    def find_open_work_order(self, agency: str, vehicle_id: str) -> Result[WorkOrder | None]: ...
    def get_work_order(self, agency: str, wo_id: str) -> Result[WorkOrder]: ...


class WriteTools(Protocol):
    """Blocked-by-policy writes return Result.blocked(...) (see results.py). reassign_block/assign_operator
    take keyword `compensation=True` when restoring the original assignment during a saga rollback: the
    adapter then skips the forward-only checks (needs / min spares / hours) but never the kill switch."""
    def create_work_order(self, agency: str, vehicle_id: str, description: str, key: str) -> Result[WorkOrder]: ...
    def cancel_work_order(self, agency: str, wo_id: str, key: str) -> Result[WorkOrder]: ...  # system-created only
    def set_vehicle_unavailable(self, agency: str, vehicle_id: str, start: datetime, end: datetime | None, key: str) -> Result[Vehicle]: ...
    def set_vehicle_available(self, agency: str, vehicle_id: str, key: str) -> Result[Vehicle]: ...
    def reassign_block(self, agency: str, block_id: str, to_vehicle_id: str, key: str, *,
                       compensation: bool = False) -> Result[Block]: ...
    def assign_operator(self, agency: str, block_id: str, operator_id: str, key: str, *,
                        compensation: bool = False) -> Result[Block]: ...
    def publish_rider_alert(self, agency: str, block_id: str, message: str, key: str) -> Result[dict]: ...  # irreversible, last


# Write -> compensation pairing (design §4.6)
COMPENSATION: dict[str, str | None] = {
    "create_work_order": "cancel_work_order",
    "set_vehicle_unavailable": "set_vehicle_available",
    "reassign_block": "reassign_block",   # back to original vehicle
    "assign_operator": "assign_operator",  # restore original operator
    "publish_rider_alert": None,           # publish a correction instead
    "set_vehicle_available": None,
}

# Saga order: irreversible step last (design §7.4)
SAGA_ORDER = [
    "create_work_order", "set_vehicle_unavailable", "reassign_block",
    "assign_operator", "publish_rider_alert",
]
