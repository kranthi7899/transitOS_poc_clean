"""Read adapters over the mock product APIs (MOCK_API.md). Native objects -> canonical objects.
Never guess: dead product, bad payload, unmapped ID -> Result.unavailable(product)."""
from __future__ import annotations

import csv
import functools
import io
from collections.abc import Callable
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from transitos.contracts.objects import Block, Needs, Operator, Vehicle, WorkOrder
from transitos.contracts.results import Result

from .common import Deps, Http, ProductUnavailable, native_of

_VEHICLE_STATUS = {
    "ready": "ready", "available": "ready", "in_service": "in_service", "active": "in_service",
    "out_of_service": "out_of_service", "unavailable": "out_of_service", "reserved": "reserved",
}
_WO_STATUS = {"open": "open", "in_progress": "in_progress", "closed": "closed",
              "cancelled": "closed", "canceled": "closed"}


def guarded(product: str) -> Callable:
    """Turn ProductUnavailable / malformed payloads into Result.unavailable (never raise to the workflow)."""
    def deco(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*a: Any, **kw: Any) -> Result:
            try:
                return fn(*a, **kw)
            except ProductUnavailable as e:
                return Result.unavailable(e.product, e.note)
            except (KeyError, ValueError, TypeError, ValidationError) as e:
                return Result.unavailable(product, f"malformed response: {type(e).__name__}: {e}"[:200])
        return wrapper
    return deco


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse_dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def _bool(s: str | None) -> bool:
    return (s or "").strip().lower() in {"1", "true", "yes", "y", "t"}


# ---- mapping helpers (shared with write adapters) -----------------------------------------------

def map_vehicle(deps: Deps, agency: str, raw: dict) -> Vehicle:
    canonical = deps.crosswalk.lookup(agency, "scheduling", raw["id"])
    if canonical is None:
        raise ProductUnavailable("crosswalk", f"unmapped scheduling vehicle {raw['id']}")
    status = _VEHICLE_STATUS.get(raw["status"])
    if status is None:
        raise ProductUnavailable("scheduling", f"unknown vehicle status {raw['status']!r}")
    return Vehicle(id=canonical, agency=agency, seats=raw["seats"],
                   wheelchair_positions=raw.get("wheelchair_positions", 0), lift_ok=raw.get("lift_ok", False),
                   type=raw.get("type", "bus"), status=status, depot=raw.get("depot", ""))  # type: ignore[arg-type]


def map_block(deps: Deps, agency: str, raw: dict) -> Block:
    vid = deps.crosswalk.lookup(agency, "scheduling", raw["vehicle"])
    if vid is None:
        raise ProductUnavailable("crosswalk", f"unmapped scheduling vehicle {raw['vehicle']}")
    op = None
    if raw.get("operator"):
        op = deps.crosswalk.lookup(agency, "workforce", raw["operator"])
        if op is None:
            raise ProductUnavailable("crosswalk", f"unmapped workforce operator {raw['operator']}")
    n = raw.get("needs") or {}
    return Block(id=raw["id"], vehicle_id=vid, operator_id=op, start=_parse_dt(raw["start"]),
                 end=_parse_dt(raw["end"]), start_depot=raw.get("start_depot", ""),
                 needs=Needs(seats=n.get("seats", 0), wheelchair_positions=n.get("wheelchair_positions", 0),
                             lift=n.get("lift", False), type=n.get("type")))


def map_work_order(deps: Deps, agency: str, raw: dict) -> WorkOrder:
    vid = deps.crosswalk.lookup(agency, "maintenance", str(raw["asset"]))
    if vid is None:
        raise ProductUnavailable("crosswalk", f"unmapped maintenance asset {raw['asset']}")
    status = _WO_STATUS.get(raw["status"])
    if status is None:
        raise ProductUnavailable("maintenance", f"unknown work order status {raw['status']!r}")
    return WorkOrder(id=str(raw["id"]), native_no=str(raw["wo_no"]), asset_vehicle_id=vid, status=status,  # type: ignore[arg-type]
                     description=raw.get("description", ""), opened_at=_parse_dt(raw.get("opened_at")),
                     closed_at=_parse_dt(raw.get("closed_at")),
                     source="human" if raw.get("opened_by") == "human" else "system")


# ---- adapters -----------------------------------------------------------------------------------

class SchedulingReader:
    """adapter id: scheduling_mock_v1"""

    def __init__(self, deps: Deps):
        self.deps = deps
        self.http = Http("scheduling", deps)

    def _native(self, agency: str, vehicle_id: str) -> str:
        n = native_of(self.deps, agency, vehicle_id, "scheduling")
        if n is None:
            raise ProductUnavailable("crosswalk", f"no scheduling id for {vehicle_id}")
        return n

    @guarded("scheduling")
    def get_vehicle(self, agency: str, vehicle_id: str) -> Result[Vehicle]:
        raw = self.http.json(agency, "GET", f"/vehicles/{self._native(agency, vehicle_id)}")
        return Result.success(map_vehicle(self.deps, agency, raw))

    @guarded("scheduling")
    def get_vehicle_history(self, agency: str, vehicle_id: str) -> Result[list[dict]]:
        """Scheduling status changes for the vehicle, plus maintenance work orders when reachable
        (partial if maintenance is down)."""
        native = self._native(agency, vehicle_id)
        changes = self.http.json(agency, "GET", "/status-changes", params={"since": "1970-01-01T00:00:00Z"})
        hist: list[dict] = [
            {"kind": "status_change", "vehicle_id": vehicle_id, "status": c["status"],
             "at": c["changed_at"], "by": c.get("changed_by")}
            for c in changes if c["vehicle"] == native]
        note = None
        mnative = native_of(self.deps, agency, vehicle_id, "maintenance")
        if mnative is not None:
            try:
                wos = Http("maintenance", self.deps).json(agency, "GET", "/workorders", params={"asset": mnative})
                hist += [{"kind": "work_order", "vehicle_id": vehicle_id, "wo_id": str(w["id"]),
                          "status": w["status"], "at": w.get("opened_at"), "by": w.get("opened_by")} for w in wos]
            except ProductUnavailable as e:
                note = f"maintenance history missing: {e.note}"
        hist.sort(key=lambda h: h.get("at") or "")
        return Result.part(hist, "maintenance", note) if note else Result.success(hist)

    def _with_assignment(self, agency: str, block: Block) -> Block:
        """Workforce is the system of record for who drives a block (scheduling's copy is not updated when
        the operator is reassigned). 404 = workforce has no record -> keep scheduling's value."""
        if "workforce" not in self.deps.clients:
            return block
        r = Http("workforce", self.deps).request(agency, "GET", f"/assignments/{block.id}", ok_4xx=True)
        if r.status_code == 404:
            return block
        if r.status_code >= 400:
            raise ProductUnavailable("workforce", f"http {r.status_code}: {r.text[:120]}")
        native = r.json().get("operator")
        op = self.deps.crosswalk.lookup(agency, "workforce", native) if native else None
        if native and op is None:
            raise ProductUnavailable("crosswalk", f"unmapped workforce operator {native}")
        return block.model_copy(update={"operator_id": op})

    @guarded("scheduling")
    def list_affected_blocks(self, agency: str, vehicle_id: str, start: datetime, end: datetime) -> Result[list[Block]]:
        raw = self.http.json(agency, "GET", "/blocks", params={
            "vehicle": self._native(agency, vehicle_id), "from": _iso(start), "to": _iso(end)})
        return Result.success([self._with_assignment(agency, map_block(self.deps, agency, b)) for b in raw])

    def get_block(self, agency: str, block_id: str) -> Result[Block]:
        """Not in ReadTools: used by write-side re-validation. MOCK_API has no GET /blocks/{id},
        so list all blocks and filter (assumption A11)."""
        return self._get_block(agency, block_id)

    @guarded("scheduling")
    def _get_block(self, agency: str, block_id: str) -> Result[Block]:
        for b in self.http.json(agency, "GET", "/blocks"):
            if b["id"] == block_id:
                return Result.success(self._with_assignment(agency, map_block(self.deps, agency, b)))
        return Result.unavailable("scheduling", f"block {block_id} not found")

    @guarded("scheduling")
    def list_ready_vehicles(self, agency: str, start: datetime, end: datetime, needs: Needs) -> Result[list[Vehicle]]:
        """Ready vehicles meeting needs and free of block assignments overlapping [start,end).
        If the blocks feed is down the result is partial (conflicts unchecked)."""
        raw = self.http.json(agency, "GET", "/vehicles", params={"status": "ready"})
        from .policy import check_spare_meets_needs
        cands = [v for v in (map_vehicle(self.deps, agency, r) for r in raw)
                 if v.status == "ready" and not check_spare_meets_needs(v, needs)]
        try:
            blocks = self.http.json(agency, "GET", "/blocks", params={"from": _iso(start), "to": _iso(end)})
        except ProductUnavailable as e:
            return Result.part(cands, "scheduling", f"assignment conflicts unchecked: {e.note}")
        busy = {b["vehicle"] for b in blocks
                if _parse_dt(b["start"]) < end and _parse_dt(b["end"]) > start}  # type: ignore[operator]
        free = [v for v in cands if native_of(self.deps, agency, v.id, "scheduling") not in busy]
        return Result.success(free)


class WorkforceReader:
    """adapter id: workforce_mock_v1. Reads come from the daily CSV export (A3) so hours may be stale."""

    def __init__(self, deps: Deps):
        self.deps = deps
        self.http = Http("workforce", deps)

    def operators(self, agency: str) -> list[Operator]:
        text = self.http.request(agency, "GET", "/export/operators.csv").text
        out: list[Operator] = []
        for row in csv.DictReader(io.StringIO(text)):
            cid = self.deps.crosswalk.lookup(agency, "workforce", row["operator"])
            if cid is None:
                continue  # unmapped operators are never offered
            out.append(Operator(
                id=cid, qualifications=[q for q in (row.get("qualifications") or "").split(";") if q],
                lift_trained=_bool(row.get("lift_trained")), hours_today=float(row.get("hours_today") or 0),
                hours_week=float(row.get("hours_week") or 0), available_from=_parse_dt(row.get("available_from")),
                available_to=_parse_dt(row.get("available_to")), on_extra_board=_bool(row.get("on_extra_board"))))
        return out

    @guarded("workforce")
    def list_eligible_operators(self, agency: str, block: Block) -> Result[list[Operator]]:
        from .policy import check_lift_qualification, check_operator_hours
        limits = self.deps.policy_for(agency).limits
        elig = []
        for op in self.operators(agency):  # the block's current operator is listed too when eligible
            if op.available_from and op.available_from > block.start:
                continue
            if op.available_to and op.available_to < block.end:
                continue
            if check_operator_hours(op.hours_today, block.start, block.end, limits):
                continue
            if check_lift_qualification(block.needs, op):
                continue
            elig.append(op)
        return Result.success(elig, note="from daily export; hours may be stale")


class MaintenanceReader:
    """adapter id: maintenance_mock_v1"""

    def __init__(self, deps: Deps):
        self.deps = deps
        self.http = Http("maintenance", deps)

    @guarded("maintenance")
    def find_open_work_order(self, agency: str, vehicle_id: str) -> Result[WorkOrder | None]:
        asset = native_of(self.deps, agency, vehicle_id, "maintenance")
        if asset is None:
            return Result.unavailable("crosswalk", f"no maintenance asset for {vehicle_id}")
        raw = self.http.json(agency, "GET", "/workorders", params={"asset": asset})
        open_ = sorted((w for w in raw if w["status"] in ("open", "in_progress")),
                       key=lambda w: w.get("opened_at") or "")
        return Result.success(map_work_order(self.deps, agency, open_[0]) if open_ else None)

    @guarded("maintenance")
    def get_work_order(self, agency: str, wo_id: str) -> Result[WorkOrder]:
        return Result.success(map_work_order(self.deps, agency, self.http.json(agency, "GET", f"/workorders/{wo_id}")))
