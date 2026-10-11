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

from transitos.clearance.evaluate import operator_items, vehicle_items
from transitos.clearance.facts import CapabilityProfile, Facts
from transitos.clearance.rules import guarded as guarded_items
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

def _blank(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


# Vehicle fields the clearance rules read, with the placeholder a canonical Vehicle needs when the source left
# them blank or absent. The placeholder is never read: the field is listed in `unknown_fields` (C-2, F3).
_VEHICLE_FIELDS: dict[str, Any] = {"seats": 0, "wheelchair_positions": 0, "lift_ok": False, "type": "bus",
                                   "length_ft": None}


def map_vehicle(deps: Deps, agency: str, raw: dict) -> Vehicle:
    canonical = deps.crosswalk.lookup(agency, "scheduling", raw["id"])
    if canonical is None:
        raise ProductUnavailable("crosswalk", f"unmapped scheduling vehicle {raw['id']}")
    status = _VEHICLE_STATUS.get(raw["status"])
    if status is None:
        raise ProductUnavailable("scheduling", f"unknown vehicle status {raw['status']!r}")
    unknown = [k for k in _VEHICLE_FIELDS if _blank(raw.get(k))]
    vals = {k: (d if k in unknown else raw[k]) for k, d in _VEHICLE_FIELDS.items()}
    return Vehicle(id=canonical, agency=agency, status=status, depot=raw.get("depot") or "",  # type: ignore[arg-type]
                   unknown_fields=unknown, **vals)


def scheduling_profile(vehicles: list[dict]) -> CapabilityProfile:
    """What the scheduling product supplies, from the payload itself (F3): a vehicle field is native when every
    vehicle record carries the key (a null or blank value is then per-record unknown), else unknown."""
    keys = set.intersection(*(set(v) for v in vehicles)) if vehicles else set()
    return CapabilityProfile("scheduling", {f: ("native" if f in keys else "unknown") for f in _VEHICLE_FIELDS})


def _needs(raw: dict | None) -> Needs:
    """Block needs. A key the block does not carry (or a blank value) is NOT declared: listed in
    `Needs.unknown_fields`, never read as "no requirement" (F3). For the two optional restrictions (`type`,
    `max_vehicle_length_ft`) an explicit null declares that there is none."""
    n = raw if isinstance(raw, dict) else {}
    unknown = [k for k in ("seats", "wheelchair_positions", "lift") if _blank(n.get(k))]
    unknown += [k for k in ("type", "max_vehicle_length_ft") if k not in n or (n[k] is not None and _blank(n[k]))]
    get = lambda k, d: d if k in unknown else (n.get(k) if n.get(k) is not None else d)
    return Needs(seats=get("seats", 0), wheelchair_positions=get("wheelchair_positions", 0), lift=get("lift", False),
                 type=get("type", None), max_vehicle_length_ft=get("max_vehicle_length_ft", None),
                 unknown_fields=unknown)


def map_block(deps: Deps, agency: str, raw: dict) -> Block:
    vid = deps.crosswalk.lookup(agency, "scheduling", raw["vehicle"])
    if vid is None:
        raise ProductUnavailable("crosswalk", f"unmapped scheduling vehicle {raw['vehicle']}")
    op = None
    if raw.get("operator"):
        op = deps.crosswalk.lookup(agency, "workforce", raw["operator"])
        if op is None:
            raise ProductUnavailable("crosswalk", f"unmapped workforce operator {raw['operator']}")
    return Block(id=raw["id"], vehicle_id=vid, operator_id=op, start=_parse_dt(raw["start"]),
                 end=_parse_dt(raw["end"]), start_depot=raw.get("start_depot", ""), needs=_needs(raw.get("needs")))


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

    def capability_profile(self, agency: str) -> CapabilityProfile:
        """What the scheduling product supplies, computed from its vehicle payload (never assumed; F3)."""
        return scheduling_profile(self.http.json(agency, "GET", "/vehicles"))

    @guarded("scheduling")
    def list_blocks(self, agency: str, start: datetime, end: datetime) -> Result[list[Block]]:
        """Every scheduled block overlapping [start,end), with the workforce assignment applied. Not in
        ReadTools: the schedule feed double-booking is checked against."""
        raw = self.http.json(agency, "GET", "/blocks", params={"from": _iso(start), "to": _iso(end)})
        return Result.success([self._with_assignment(agency, map_block(self.deps, agency, b)) for b in raw])

    @guarded("scheduling")
    def list_ready_vehicles(self, agency: str, start: datetime, end: datetime, needs: Needs) -> Result[list[Vehicle]]:
        """Ready vehicles meeting needs (graded type fit: a larger class is kept, see clearance) and free of
        block assignments overlapping [start,end). If the blocks feed is down the result is partial."""
        raw = self.http.json(agency, "GET", "/vehicles", params={"status": "ready"})
        policy = self.deps.policy_for(agency)
        probe = Block(id="-", vehicle_id="-", start=start, end=end, needs=needs)
        facts = Facts(blocks={}, vehicles={}, operators={}, ready_count=None, commitments=None,
                      profiles={"scheduling": scheduling_profile(raw)})
        cands = [v for v in (map_vehicle(self.deps, agency, r) for r in raw)   # one bad record excludes only it (F4)
                 if v.status == "ready"
                 and not any(i.status == "violation"
                             for i in guarded_items(f"vehicle {v.id}", vehicle_items, probe, v, facts, policy))]
        try:
            blocks = self.http.json(agency, "GET", "/blocks", params={"from": _iso(start), "to": _iso(end)})
        except ProductUnavailable as e:
            return Result.part(cands, "scheduling", f"assignment conflicts unchecked: {e.note}")
        busy = {b["vehicle"] for b in blocks
                if _parse_dt(b["start"]) < end and _parse_dt(b["end"]) > start}  # type: ignore[operator]
        free = [v for v in cands if native_of(self.deps, agency, v.id, "scheduling") not in busy]
        return Result.success(free)


# Which export columns carry each capability-profile field. A field is supplied natively only when every one
# of its columns is present in the export's header row.
FIELD_COLUMNS: dict[str, tuple[str, ...]] = {
    "qualifications": ("qualifications",), "licence_class": ("licence_class",), "endorsements": ("endorsements",),
    "trained_vehicle_types": ("trained_vehicle_types",), "training_valid_to": ("training_valid_to",),
    "lift_trained": ("lift_trained",), "hours_today": ("hours_today",), "hours_week": ("hours_week",),
    "availability": ("available_from", "available_to"), "duty_times": ("last_duty_end", "duty_start_today"),
    "medical_valid_to": ("medical_valid_to",),
}


# The export columns that carry a contract field of the same name (all but `operator` and `on_extra_board`).
OPERATOR_COLUMNS = tuple(c for cols in FIELD_COLUMNS.values() for c in cols)


NONE_TOKEN = "none"   # the export's explicit "holds none" for a list column (A129); a BLANK cell is unknown


def _split(s: str | None) -> list[str]:
    if (s or "").strip().lower() == NONE_TOKEN:
        return []
    return [x for x in (s or "").split(";") if x]


def _dated(s: str | None) -> dict[str, datetime]:
    """'P=2027-01-01T00:00:00+00:00;lift=...' -> {code: datetime}"""
    out: dict[str, datetime] = {}
    for part in _split(s):
        k, _, v = part.partition("=")
        if k and v:
            out[k] = datetime.fromisoformat(v)
    return out


class WorkforceReader:
    """adapter id: workforce_mock_v1. Reads come from the daily CSV export (A3) so hours may be stale.
    The header row decides which fields the export supplies (capability profile, Phase 10, A73): a column that
    is absent is unknown for every operator, never a default value."""

    def __init__(self, deps: Deps):
        self.deps = deps
        self.http = Http("workforce", deps)

    def _export(self, agency: str) -> tuple[list[Operator], CapabilityProfile]:
        text = self.http.request(agency, "GET", "/export/operators.csv").text
        reader = csv.DictReader(io.StringIO(text))
        cols = set(reader.fieldnames or [])
        profile = CapabilityProfile("workforce", {
            f: ("native" if set(c) <= cols else "unknown") for f, c in FIELD_COLUMNS.items()})
        out: list[Operator] = []
        for row in reader:
            cid = self.deps.crosswalk.lookup(agency, "workforce", row["operator"])
            if cid is None:
                continue  # unmapped operators are never offered
            # a column absent from the export or a blank cell is unknown for THIS operator (C-2, F3): the value
            # below is only a placeholder the rules never read
            unknown = [c for c in OPERATOR_COLUMNS if _blank(row.get(c))]
            out.append(Operator(
                id=cid, qualifications=_split(row.get("qualifications")),
                lift_trained=_bool(row.get("lift_trained")), hours_today=float(row.get("hours_today") or 0),
                hours_week=float(row.get("hours_week") or 0), available_from=_parse_dt(row.get("available_from")),
                available_to=_parse_dt(row.get("available_to")), on_extra_board=_bool(row.get("on_extra_board")),
                licence_class=(row.get("licence_class") or None),  # type: ignore[arg-type]
                endorsements=_split(row.get("endorsements")),
                trained_vehicle_types=_split(row.get("trained_vehicle_types")),
                training_valid_to=_dated(row.get("training_valid_to")),
                medical_valid_to=_parse_dt(row.get("medical_valid_to")),
                last_duty_end=_parse_dt(row.get("last_duty_end")),
                duty_start_today=_parse_dt(row.get("duty_start_today")), unknown_fields=unknown))
        return out, profile

    def operators(self, agency: str) -> list[Operator]:
        return self._export(agency)[0]

    def capability_profile(self, agency: str) -> CapabilityProfile:
        return self._export(agency)[1]

    @guarded("workforce")
    def list_eligible_operators(self, agency: str, block: Block) -> Result[list[Operator]]:
        """Operators with no KNOWN violation of a hard rule for this block (unknown data keeps them listed;
        the workflow ranks and flags them). Qualification is judged on the block's required type here; the
        planner re-judges it against the vehicle actually assigned."""
        policy = self.deps.policy_for(agency)
        ops, profile = self._export(agency)
        facts = Facts(blocks={block.id: block}, vehicles={}, operators={}, ready_count=None, commitments=None,
                      profiles={"workforce": profile})
        elig = [op for op in ops  # the block's current operator is listed too when eligible; a record that breaks
                if not any(i.status == "violation"   # a rule (F4) excludes only that operator
                           for i in guarded_items(f"operator {op.id}", operator_items, block, op, block.needs.type,
                                                  facts, policy))]
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
