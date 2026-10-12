"""Maintenance, scheduling and workforce pollers (design §2.2 publishers)."""
from __future__ import annotations

from transitos.contracts import ids

from .base import (
    Emitted,
    Poller,
    RecordUnreadable,
    cursor_instant,
    cursor_str,
    later,
    make_event,
    raw_key,
)


def _norm(cursor: str | None) -> str | None:
    """A stored cursor as RFC 3339 UTC (bug sweep B fix round N1 b, A166). A legacy cursor without a zone restarts
    from the beginning (a re-read is de-duplicated; a skip would lose events)."""
    dt = cursor_instant(cursor)
    return None if dt is None else cursor_str(dt)


class MaintenancePoller(Poller):
    """PM transition (not overdue -> overdue) => PMOverdue; WO first seen => WorkOrderOpened; WO closed => WorkOrderClosed.
    Cursors (A166): compared as INSTANTS, moved only by records that were read, and never past the earliest open
    reject of their stream, so a rejected record that is later corrected is always read again."""

    product = "maintenance"

    def build(self, cursor, last_seen):
        seen = dict(last_seen)
        pm_seen: dict = dict(seen.get("pm", {}))
        wo_seen: dict = dict(seen.get("wo", {}))
        out: list[Emitted] = []
        pm_since = pm_cursor = _norm(seen.get("pm_cursor"))
        wo_since = wo_cursor = _norm(seen.get("wo_cursor"))
        ident: dict = {}   # Phase 10 Step 2: the asset's VIN, plate and name, read once per poll

        for pm in self.get("/pm-schedules", updated_since=pm_since):
            k = f"{pm['asset']}:{pm['service_code']}:{pm['due_mi']}"
            overdue = pm["meter_mi"] > pm["due_mi"]
            ev = None
            try:
                at = self.instant(pm, "updated_at") if pm.get("updated_at") else None
                if overdue and not pm_seen.get(k):
                    ev = make_event(
                        self.agency, self.product, ids.pm_id(self.agency, pm["asset"], pm["service_code"], pm["due_mi"]),
                        "PMOverdue", self.ts(pm, "updated_at"),
                        {"native_asset_id": pm["asset"], "service_code": pm["service_code"],
                         "due_mi": pm["due_mi"], "meter_mi": pm["meter_mi"],
                         **self.evidence(ident, f"/assets/{pm['asset']}")})
            except RecordUnreadable as e:   # bug sweep B (A155): this record only; its seen state stays as it was
                self.reject(f"pm:{k}", pm["asset"], pm, e, stream="pm", floor=pm_since)
                continue
            if ev is not None:
                out.append((ev, raw_key(self.product, pm["asset"])))
            if at is not None:
                pm_cursor = later(pm_cursor, at)
            self.accept(f"pm:{k}")
            pm_seen[k] = overdue

        for wo in self.get("/workorders", updated_since=wo_since):
            prev = wo_seen.get(wo["id"])
            asset, wo_no = wo["asset"], wo["wo_no"]
            key = raw_key(self.product, asset)
            status = str(wo["status"]).lower()   # Step 4 (A107): shop vocabulary; only CLOSED/closed is a close
            evs: list[Emitted] = []
            try:
                at = self.instant(wo, "updated_at") if wo.get("updated_at") else None
                if prev is None:
                    evs.append((make_event(
                        self.agency, self.product, ids.wo_open_id(self.agency, wo_no), "WorkOrderOpened",
                        self.ts(wo, "opened_at" if wo.get("opened_at") else "updated_at"),
                        {"native_asset_id": asset, "wo_no": wo_no, "opened_by": wo.get("opened_by", "human"),
                         **self.evidence(ident, f"/assets/{asset}")}), key))
                # A cancelled WO (our own saga compensation; mock: status=closed + cancelled=true, A's A12)
                # is not a repair: no WorkOrderClosed, so no run is resumed for return-to-service.
                if status == "closed" and prev != "closed" and not wo.get("cancelled"):
                    evs.append((make_event(
                        self.agency, self.product, ids.wo_close_id(self.agency, wo_no), "WorkOrderClosed",
                        self.ts(wo, "closed_at" if wo.get("closed_at") else "updated_at"),
                        {"native_asset_id": asset, "wo_no": wo_no, **self.evidence(ident, f"/assets/{asset}")}), key))
            except RecordUnreadable as e:   # bug sweep B (A155): none of this record's events; seen state unchanged
                self.reject(f"wo:{wo['id']}", wo["id"], wo, e, stream="wo", floor=wo_since)
                continue
            if at is not None:
                wo_cursor = later(wo_cursor, at)
            self.accept(f"wo:{wo['id']}")
            out += evs
            wo_seen[wo["id"]] = status

        return out, None, {"pm": pm_seen, "wo": wo_seen, "pm_cursor": self.capped("pm", pm_cursor),
                           "wo_cursor": self.capped("wo", wo_cursor)}


class SchedulingPoller(Poller):
    """GET /status-changes?since= -> VehicleStatusChanged."""

    product = "scheduling"

    def build(self, cursor, last_seen):
        out: list[Emitted] = []
        since = new_cursor = _norm(cursor)
        ident: dict = {}   # Phase 10 Step 2: the vehicle's VIN, plate and name, read once per poll
        for ch in self.get("/status-changes", since=since):
            rk = f"change:{ch['vehicle']}:{ch['status']}:{ch['changed_at']}"
            try:
                at = self.instant(ch, "changed_at")
                ev = make_event(
                    self.agency, self.product, ids.vstatus_id(self.agency, ch["vehicle"], ch["status"], ch["changed_at"]),
                    "VehicleStatusChanged", self.ts(ch, "changed_at"),
                    {"native_vehicle_id": ch["vehicle"], "status": ch["status"], "changed_by": ch.get("changed_by", ""),
                     **self.evidence(ident, f"/vehicles/{ch['vehicle']}")})
            except RecordUnreadable as e:   # bug sweep B (A155)
                self.reject(rk, ch["vehicle"], ch, e, stream="changes", floor=since, subject=ch["vehicle"])
                continue
            new_cursor = later(new_cursor, at)   # A166
            self.accept(rk)
            self.supersede(ch["vehicle"], rk)    # round 3 (A177): a later change for the vehicle supersedes
            out.append((ev, raw_key(self.product, ch["vehicle"])))
        return out, self.capped("changes", new_cursor), last_seen


class WorkforcePoller(Poller):
    """GET /availability-changes?since= -> OperatorAvailabilityChanged.
    Item shape is not fixed in MOCK_API.md; assumed A12: {operator, status, changed_at}."""

    product = "workforce"

    def build(self, cursor, last_seen):
        out: list[Emitted] = []
        since = new_cursor = _norm(cursor)
        for ch in self.get("/availability-changes", since=since):
            rk = f"change:{ch['operator']}:{ch['status']}:{ch['changed_at']}"
            try:
                at = self.instant(ch, "changed_at")
                ev = make_event(
                    self.agency, self.product, ids.operator_avail_id(self.agency, ch["operator"], ch["status"], ch["changed_at"]),
                    "OperatorAvailabilityChanged", self.ts(ch, "changed_at"),
                    {"native_operator_id": ch["operator"], "status": ch["status"]})
            except RecordUnreadable as e:   # bug sweep B (A155)
                self.reject(rk, ch["operator"], ch, e, stream="changes", floor=since, subject=ch["operator"])
                continue
            new_cursor = later(new_cursor, at)   # A166
            self.accept(rk)
            self.supersede(ch["operator"], rk)   # round 3 (A177): a later change for the operator supersedes
            out.append((ev, raw_key(self.product, ch["operator"])))
        return out, self.capped("changes", new_cursor), last_seen
