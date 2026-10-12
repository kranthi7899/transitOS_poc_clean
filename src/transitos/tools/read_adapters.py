"""Read adapters over the mock product APIs (MOCK_API.md). Native objects -> canonical objects.
Never guess: dead product, bad payload, unmapped ID -> Result.unavailable(product)."""
from __future__ import annotations

import csv
import functools
import io
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from transitos.clearance.evaluate import operator_items, vehicle_items
from transitos.clearance.facts import CapabilityProfile, Facts
from transitos.clearance.rules import guarded as guarded_items
from transitos.clearance.rules import known_need_type
from transitos.contracts.objects import Block, Needs, Operator, Vehicle, WorkOrder
from transitos.contracts.results import Result

from .common import (
    Deps,
    Http,
    ProductUnavailable,
    UnknownTimezone,
    local_time,
    maintenance_mode,
    native_of,
    to_utc,
)

_VEHICLE_STATUS = {
    "ready": "ready", "available": "ready", "in_service": "in_service", "active": "in_service",
    "out_of_service": "out_of_service", "unavailable": "out_of_service", "reserved": "reserved",
}
_WO_STATUS = {"open": "open", "in_progress": "in_progress", "closed": "closed",
              "cancelled": "closed", "canceled": "closed",
              # Phase 10 Step 4 shop vocabulary (A23 extended, A107): only CLOSED is closed. Parts wait and work
              # done but not signed off are still being worked; a repair deferred back to pending is open.
              "OPEN": "open", "IN_PROGRESS": "in_progress", "WAITING_PARTS": "in_progress", "PENDING": "open",
              "COMPLETE": "in_progress", "CLOSED": "closed"}
_PR_STATUS = {"OPEN": "open", "CANCELLED": "closed"}   # a CONVERTED request takes its work order's status (A106)


def guarded(product: str) -> Callable:
    """Turn ProductUnavailable / malformed payloads into Result.unavailable (never raise to the workflow)."""
    def deco(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*a: Any, **kw: Any) -> Result:
            try:
                return fn(*a, **kw)
            except ProductUnavailable as e:
                return Result.unavailable(e.product, e.note)
            except UnknownTimezone as e:   # Step 4.2 (A108): zone-less time with no known zone is never read as UTC
                return Result.unavailable(product, f"timezone_unknown: {e}"[:200])
            except (KeyError, ValueError, TypeError, ValidationError) as e:
                return Result.unavailable(product, f"malformed response: {type(e).__name__}: {e}"[:200])
        return wrapper
    return deco


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse_dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def _zoned_dt(s: str | None) -> datetime | None:
    """A product timestamp that must carry its zone. A zone-less one is never guessed as UTC (A108)."""
    dt = _parse_dt(s)
    if dt is not None and (dt.tzinfo is None or dt.utcoffset() is None):
        raise ValueError(f"zone-less timestamp {s!r} and the agency has no local_time setting: zone unknown (A108)")
    return dt


def _time_reader(deps: Any, agency: str, *, strict: bool = True) -> Callable[..., datetime | None]:
    """How this agency's maintenance product and workforce CSV write time (Step 4.2, A108/A109): zoned (default)
    or zone-less local in the agency timezone. A blank value is unknown (None), never now() and never UTC.
    Knob off: `strict` refuses a zone-less value (maintenance); `strict=False` keeps the pre-Step-4 parse for the
    workforce CSV, whose zone-less cell already fails closed per operator inside clearance (F4; known gap A113)."""
    local, tz = local_time(deps, agency)

    def read(s: Any, utc_offset: str | None = None, fold: int = 0) -> datetime | None:
        if _blank(s):
            return None
        if local:   # `fold` matters only in the repeated fall-back hour and only when no utc_offset came (A145)
            return to_utc(str(s).strip(), tz, utc_offset=utc_offset, fold=fold)  # type: ignore[arg-type]
        return _zoned_dt(str(s)) if strict else _parse_dt(str(s))
    return read


def _offsets(raw: dict) -> dict:
    o = raw.get("utc_offset")
    return o if isinstance(o, dict) else {}


def _utc_str(dt: datetime | None) -> str | None:
    return None if dt is None else dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


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
    vehicle record carries the key (a null or blank value is then per-record unknown), else unknown (A131)."""
    keys = set.intersection(*(set(v) for v in vehicles)) if vehicles else set()
    return CapabilityProfile("scheduling", {f: ("native" if f in keys else "unknown") for f in _VEHICLE_FIELDS})


def _needs(raw: dict | None) -> Needs:
    """Block needs. A key the block does not carry (or a blank value) is NOT declared: listed in
    `Needs.unknown_fields`, never read as "no requirement" (F3). For the two optional restrictions (`type`,
    `max_vehicle_length_ft`) an explicit null declares that there is none (A130)."""
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
    read, off = _time_reader(deps, agency), _offsets(raw)
    return WorkOrder(id=str(raw["id"]), native_no=str(raw["wo_no"]), asset_vehicle_id=vid, status=status,  # type: ignore[arg-type]
                     description=raw.get("description", ""), opened_at=read(raw.get("opened_at"), off.get("opened_at")),
                     closed_at=read(raw.get("closed_at"), off.get("closed_at")),
                     source="human" if raw.get("opened_by") == "human" else "system")


def map_pending_repair(deps: Deps, agency: str, pr: dict, wo: dict | None = None) -> WorkOrder:
    """Phase 10 Step 4.1 (A106): a pending repair as the canonical WorkOrder the workflow tracks. Its id stays the
    REQUEST's id for life (one effect per idempotency key; compensation cancels through it). Once the foreman has
    put it on a work order (`wo`), number, status and dates are that work order's. A cancelled request is closed."""
    vid = deps.crosswalk.lookup(agency, "maintenance", str(pr["asset"]))
    if vid is None:
        raise ProductUnavailable("crosswalk", f"unmapped maintenance asset {pr['asset']}")
    read = _time_reader(deps, agency)
    src = "human" if pr.get("requested_by") == "human" else "system"
    if pr["status"] == "CONVERTED" and wo is not None:
        w = map_work_order(deps, agency, wo)
        return w.model_copy(update={"id": str(pr["id"]), "source": src})
    status = _PR_STATUS.get(pr["status"])
    if status is None:
        raise ProductUnavailable("maintenance", f"unknown pending repair status {pr['status']!r}")
    off = _offsets(pr)
    return WorkOrder(id=str(pr["id"]), native_no=str(pr["pr_no"]), asset_vehicle_id=vid, status=status,  # type: ignore[arg-type]
                     description=pr.get("description", ""), opened_at=read(pr.get("created_at"), off.get("created_at")),
                     closed_at=read(pr.get("updated_at"), off.get("updated_at")) if status == "closed" else None,
                     source=src)  # type: ignore[arg-type]


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
                local, read = local_time(self.deps, agency)[0], _time_reader(self.deps, agency)
                # Step 4.2: a zone-less local opened_at is passed on as UTC (never naive); default: unchanged
                hist += [{"kind": "work_order", "vehicle_id": vehicle_id, "wo_id": str(w["id"]),
                          "status": w["status"], "by": w.get("opened_by"),
                          "at": _utc_str(read(w.get("opened_at"), _offsets(w).get("opened_at"))) if local
                          else w.get("opened_at")} for w in wos]
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


# Fix round F2 (A145): the workforce CSV carries no utc_offset, so a wall time in the repeated fall-back hour is read
# as the CONSERVATIVE occurrence for the rule it feeds: the LATER one (fold=1) for a start of availability and for
# the end of the last duty (less availability, less rest); the EARLIER one (fold=0) for an end of availability, the
# start of today's duty (more spread) and every expiry (training_valid_to via _dated, medical_valid_to).
CSV_FOLD = {"available_from": 1, "last_duty_end": 1, "available_to": 0, "duty_start_today": 0,
            "medical_valid_to": 0, "training_valid_to": 0}


def _dated(s: str | None, read: Callable[..., datetime | None] = _parse_dt) -> dict[str, datetime]:
    """'P=2027-01-01T00:00:00+00:00;lift=...' -> {code: datetime}"""
    out: dict[str, datetime] = {}
    for part in _split(s):
        k, _, v = part.partition("=")
        if k and v and (dt := read(v)) is not None:
            out[k] = dt
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
        # Step 4.2: the CSV may carry zone-less local time (no offsets); knob off = the pre-Step-4 parse (A113)
        ts = _time_reader(self.deps, agency, strict=False)
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
                hours_week=float(row.get("hours_week") or 0),
                available_from=ts(row.get("available_from"), fold=CSV_FOLD["available_from"]),
                available_to=ts(row.get("available_to"), fold=CSV_FOLD["available_to"]),
                on_extra_board=_bool(row.get("on_extra_board")),
                licence_class=(row.get("licence_class") or None),  # type: ignore[arg-type]
                endorsements=_split(row.get("endorsements")),
                trained_vehicle_types=_split(row.get("trained_vehicle_types")),
                training_valid_to=_dated(row.get("training_valid_to"), ts),
                medical_valid_to=ts(row.get("medical_valid_to"), fold=CSV_FOLD["medical_valid_to"]),
                last_duty_end=ts(row.get("last_duty_end"), fold=CSV_FOLD["last_duty_end"]),
                duty_start_today=ts(row.get("duty_start_today"), fold=CSV_FOLD["duty_start_today"]),
                unknown_fields=unknown))
        return out, profile

    def operators(self, agency: str) -> list[Operator]:
        return self._export(agency)[0]

    def capability_profile(self, agency: str) -> CapabilityProfile:
        # the header row alone (Step 4.2: a cell that cannot be read, e.g. local time with no known zone, makes the
        # operator read unavailable, not the profile of what the export supplies)
        text = self.http.request(agency, "GET", "/export/operators.csv").text
        cols = set(csv.DictReader(io.StringIO(text)).fieldnames or [])
        return CapabilityProfile("workforce", {
            f: ("native" if set(c) <= cols else "unknown") for f, c in FIELD_COLUMNS.items()})

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
                           for i in guarded_items(f"operator {op.id}", operator_items, block, op, known_need_type(block),
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
        read = _time_reader(self.deps, agency)
        # open = any status the map calls open or in progress (shop vocabulary: WAITING_PARTS, PENDING, COMPLETE are
        # not closed, A107); earliest first, by the INSTANT (a local wall-clock string does not sort across DST)
        open_ = sorted(((read(w.get("opened_at"), _offsets(w).get("opened_at")), i, w) for i, w in enumerate(raw)
                        if _WO_STATUS.get(w["status"]) in ("open", "in_progress")),
                       key=lambda t: (t[0] is not None, t[0] or datetime.min.replace(tzinfo=UTC), t[1]))
        if open_:
            return Result.success(map_work_order(self.deps, agency, open_[0][2]))
        if maintenance_mode(self.deps, agency) == "pending_repair":   # Step 4.1: a request not yet on a WO (A106)
            prs = self.http.json(agency, "GET", "/pending-repairs", params={"asset": asset, "status": "OPEN"})
            if prs:
                return Result.success(map_pending_repair(self.deps, agency, prs[0]))
        return Result.success(None)

    # ---- Phase 10 Step 4.1: pending repairs (A105, A106) -------------------------------------------------------
    def _pr_raw(self, agency: str, pr_id: str) -> dict | None:
        """The pending repair with this id, or None when the product has none (a work order id, say)."""
        r = self.http.request(agency, "GET", f"/pending-repairs/{pr_id}", ok_4xx=True)
        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            raise ProductUnavailable("maintenance", f"http {r.status_code}: {r.text[:120]}")
        return r.json()

    def resolve_pending(self, agency: str, pr: dict) -> WorkOrder:
        wo = None
        if pr.get("status") == "CONVERTED" and pr.get("work_order"):
            wo = self.http.json(agency, "GET", f"/workorders/{pr['work_order']}")
        return map_pending_repair(self.deps, agency, pr, wo)

    @guarded("maintenance")
    def pending_by_key(self, agency: str, key: str) -> Result[dict | None]:
        """The pending repair created with this idempotency key (raw product record), or None."""
        found = self.http.json(agency, "GET", "/pending-repairs", params={"idempotency_key": key})
        return Result.success(found[0] if found else None)

    def asset_statuses(self, agency: str, vehicle_ids: list[str]) -> Result[dict[str, str]]:
        """S2 fix round F4 (A142), optional read: each vehicle's MAINTENANCE asset status via the crosswalk.
        The product's own status ("active", "retired", ...); "missing" when the crosswalk has no maintenance asset
        for the vehicle or the product has no such asset; "unreachable" when the product does not answer. Never
        raises: an unknown status is the clearance rule's to judge (unverifiable)."""
        out: dict[str, str] = {}
        for vid in sorted(set(vehicle_ids)):
            asset = native_of(self.deps, agency, vid, "maintenance")
            if asset is None:
                out[vid] = "missing"
                continue
            try:
                r = self.http.request(agency, "GET", f"/assets/{asset}", ok_4xx=True)
            except ProductUnavailable:
                out[vid] = "unreachable"
                continue
            if r.status_code == 404:
                out[vid] = "missing"
            elif r.status_code >= 400:
                out[vid] = "unreachable"
            else:
                out[vid] = str((r.json() or {}).get("status") or "")
        return Result.success(out)

    @guarded("maintenance")
    def get_work_order(self, agency: str, wo_id: str) -> Result[WorkOrder]:
        if maintenance_mode(self.deps, agency) == "pending_repair" and (pr := self._pr_raw(agency, wo_id)) is not None:
            return Result.success(self.resolve_pending(agency, pr))   # Step 4.1: a request id resolves through
        return Result.success(map_work_order(self.deps, agency, self.http.json(agency, "GET", f"/workorders/{wo_id}")))
