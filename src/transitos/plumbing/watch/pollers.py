"""Maintenance, scheduling and workforce pollers (design §2.2 publishers)."""
from __future__ import annotations

from transitos.contracts import ids

from .base import Emitted, Poller, make_event, raw_key


def _max(a: str | None, b: str | None) -> str | None:
    return max([x for x in (a, b) if x], default=None)


class MaintenancePoller(Poller):
    """PM transition (not overdue -> overdue) => PMOverdue; WO first seen => WorkOrderOpened; WO closed => WorkOrderClosed."""

    product = "maintenance"

    def build(self, cursor, last_seen):
        seen = dict(last_seen)
        pm_seen: dict = dict(seen.get("pm", {}))
        wo_seen: dict = dict(seen.get("wo", {}))
        out: list[Emitted] = []
        pm_cursor = seen.get("pm_cursor")
        wo_cursor = seen.get("wo_cursor")
        ident: dict = {}   # Phase 10 Step 2: the asset's VIN, plate and name, read once per poll

        for pm in self.get("/pm-schedules", updated_since=pm_cursor):
            k = f"{pm['asset']}:{pm['service_code']}:{pm['due_mi']}"
            overdue = pm["meter_mi"] > pm["due_mi"]
            if overdue and not pm_seen.get(k):
                out.append((make_event(
                    self.agency, self.product, ids.pm_id(self.agency, pm["asset"], pm["service_code"], pm["due_mi"]),
                    "PMOverdue", self.ts(pm, "updated_at"),
                    {"native_asset_id": pm["asset"], "service_code": pm["service_code"],
                     "due_mi": pm["due_mi"], "meter_mi": pm["meter_mi"],
                     **self.evidence(ident, f"/assets/{pm['asset']}")}),
                    raw_key(self.product, pm["asset"])))
            pm_seen[k] = overdue
            pm_cursor = _max(pm_cursor, pm.get("updated_at"))

        for wo in self.get("/workorders", updated_since=wo_cursor):
            prev = wo_seen.get(wo["id"])
            asset, wo_no = wo["asset"], wo["wo_no"]
            key = raw_key(self.product, asset)
            status = str(wo["status"]).lower()   # Step 4 (A107): shop vocabulary; only CLOSED/closed is a close
            if prev is None:
                out.append((make_event(
                    self.agency, self.product, ids.wo_open_id(self.agency, wo_no), "WorkOrderOpened",
                    self.ts(wo, "opened_at" if wo.get("opened_at") else "updated_at"),
                    {"native_asset_id": asset, "wo_no": wo_no, "opened_by": wo.get("opened_by", "human"),
                     **self.evidence(ident, f"/assets/{asset}")}), key))
            # A cancelled WO (our own saga compensation; mock: status=closed + cancelled=true, A's A12)
            # is not a repair: no WorkOrderClosed, so no run is resumed for return-to-service.
            if status == "closed" and prev != "closed" and not wo.get("cancelled"):
                out.append((make_event(
                    self.agency, self.product, ids.wo_close_id(self.agency, wo_no), "WorkOrderClosed",
                    self.ts(wo, "closed_at" if wo.get("closed_at") else "updated_at"),
                    {"native_asset_id": asset, "wo_no": wo_no, **self.evidence(ident, f"/assets/{asset}")}), key))
            wo_seen[wo["id"]] = status
            wo_cursor = _max(wo_cursor, wo.get("updated_at"))

        return out, None, {"pm": pm_seen, "wo": wo_seen, "pm_cursor": pm_cursor, "wo_cursor": wo_cursor}


class SchedulingPoller(Poller):
    """GET /status-changes?since= -> VehicleStatusChanged."""

    product = "scheduling"

    def build(self, cursor, last_seen):
        out: list[Emitted] = []
        new_cursor = cursor
        ident: dict = {}   # Phase 10 Step 2: the vehicle's VIN, plate and name, read once per poll
        for ch in self.get("/status-changes", since=cursor):
            out.append((make_event(
                self.agency, self.product, ids.vstatus_id(self.agency, ch["vehicle"], ch["status"], ch["changed_at"]),
                "VehicleStatusChanged", self.ts(ch, "changed_at"),
                {"native_vehicle_id": ch["vehicle"], "status": ch["status"], "changed_by": ch.get("changed_by", ""),
                 **self.evidence(ident, f"/vehicles/{ch['vehicle']}")}),
                raw_key(self.product, ch["vehicle"])))
            new_cursor = _max(new_cursor, ch["changed_at"])
        return out, new_cursor, last_seen


class WorkforcePoller(Poller):
    """GET /availability-changes?since= -> OperatorAvailabilityChanged.
    Item shape is not fixed in MOCK_API.md; assumed A12: {operator, status, changed_at}."""

    product = "workforce"

    def build(self, cursor, last_seen):
        out: list[Emitted] = []
        new_cursor = cursor
        for ch in self.get("/availability-changes", since=cursor):
            out.append((make_event(
                self.agency, self.product, ids.operator_avail_id(self.agency, ch["operator"], ch["status"], ch["changed_at"]),
                "OperatorAvailabilityChanged", self.ts(ch, "changed_at"),
                {"native_operator_id": ch["operator"], "status": ch["status"]}),
                raw_key(self.product, ch["operator"])))
            new_cursor = _max(new_cursor, ch["changed_at"])
        return out, new_cursor, last_seen
