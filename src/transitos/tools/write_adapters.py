"""Write adapters. Every write: (1) kill-switch gate, (2) idempotent replay, (3) hard-limit re-check,
(4) product call, (5) read-back. Blocked writes return Result.blocked(...) (contracts/results.py:
status unavailable, product "policy", note "kill_switch: ..." | "hard_limit: ...").

Reassign/assign accept `compensation=True` (keyword, optional) to skip needs/spares/hours checks when
restoring the original assignment during a saga rollback. Everything else is checked."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from transitos.clearance.evaluate import (
    Assign,
    Option,
    double_booking_item,
    kill_switch_item,
    operator_items,
    safe_vehicle_move,
)
from transitos.clearance.facts import Facts, commitments_from_blocks
from transitos.clearance.rules import count_ready, guarded, known_vehicle_type
from transitos.contracts.events import SYSTEM_ACTOR
from transitos.contracts.objects import Block, Needs, Vehicle, WorkOrder
from transitos.contracts.results import HARD_LIMIT, KILL_SWITCH, Result

from .common import Deps, Http, ProductUnavailable, maintenance_mode, native_of
from .policy import kill_switch
from .read_adapters import (
    MaintenanceReader,
    SchedulingReader,
    WorkforceReader,
    _iso,
    map_block,
    map_pending_repair,
    map_work_order,
)


class HardLimit(Exception):
    def __init__(self, violations: list[str]):
        self.violations = violations


class _Base:
    product = "?"

    def __init__(self, deps: Deps):
        self.deps = deps
        self.http = Http(self.product, deps)

    def _run(self, agency: str, tool: str, key: str, fn: Callable[[], Result], *, idem: bool = True) -> Result:
        policy = self.deps.policy_for(agency)
        if kill_switch_item(kill_switch(agency, self.deps.killswitch, policy)).status == "violation":
            return Result.blocked(KILL_SWITCH, f"writes disabled for {agency}")
        if idem and (prior := self.deps.idem.get(agency, tool, key)) is not None:
            return prior
        try:
            res = fn()
        except HardLimit as h:
            return Result.blocked(HARD_LIMIT, "; ".join(h.violations))
        except ProductUnavailable as e:
            return Result.unavailable(e.product, e.note)
        except (KeyError, ValueError, TypeError) as e:
            return Result.unavailable(self.product, f"malformed response: {type(e).__name__}: {e}"[:200])
        if idem:
            self.deps.idem.put(agency, tool, key, res)
        return res

    def _native_vehicle(self, agency: str, vehicle_id: str, product: str) -> str:
        n = native_of(self.deps, agency, vehicle_id, product)
        if n is None:
            raise ProductUnavailable("crosswalk", f"no {product} id for {vehicle_id}")
        return n


class MaintenanceWriter(_Base):
    """adapter id: maintenance_mock_v1. Product honours Idempotency-Key (A1), so no local store."""
    product = "maintenance"

    def __init__(self, deps: Deps):
        super().__init__(deps)
        self.reader = MaintenanceReader(deps)

    def create_work_order(self, agency: str, vehicle_id: str, description: str, key: str) -> Result[WorkOrder]:
        def go() -> Result[WorkOrder]:
            asset = self._native_vehicle(agency, vehicle_id, "maintenance")
            pending = maintenance_mode(self.deps, agency) == "pending_repair"
            if pending:   # Step 4.1 (A106): a replay of this key reads back OUR request, converted or not
                mine = self.reader.pending_by_key(agency, key)
                if mine.status != "ok":
                    return mine  # type: ignore[return-value]
                if mine.value is not None:
                    return self._pending_result(agency, mine.value)
            existing = self.reader.find_open_work_order(agency, vehicle_id)
            if existing.status == "unavailable":
                return existing  # type: ignore[return-value]
            if existing.value is not None:  # reuse (typically human-opened) WO; never duplicate
                return Result.success(existing.value, note="already_open")
            if pending:
                return self._create_pending(agency, asset, description, key)
            try:
                raw = self.http.json(agency, "POST", "/workorders", headers={"Idempotency-Key": key},
                                     json={"asset": asset, "description": description, "opened_by": "system"})
            except ProductUnavailable as e:   # Phase 10 Step 2 (A87): a retired asset is a refusal, not an outage
                if (e.note or "").startswith("http 409") and "retired" in (e.note or ""):
                    raise HardLimit([f"asset_retired:{asset}"]) from e
                raise
            back = self.reader.get_work_order(agency, str(raw["id"]))  # read back
            if back.status != "ok":
                return Result.part(map_work_order(self.deps, agency, raw), "maintenance",
                                   f"readback failed: {back.note}")
            return back
        return self._run(agency, "create_work_order", key, go, idem=False)

    # ---- Phase 10 Step 4.1: `maintenance.mode: pending_repair` (A105, A106) ------------------------------------
    def _pending_result(self, agency: str, pr: dict) -> Result[WorkOrder]:
        wo = self.reader.resolve_pending(agency, pr)
        note = (f"pending_repair {pr['pr_no']} on work order {wo.native_no}" if pr.get("status") == "CONVERTED"
                else f"pending_repair {pr['pr_no']} {pr.get('status')}: not yet on a work order")
        return Result.success(wo, note=note)

    def _create_pending(self, agency: str, asset: str, description: str, key: str) -> Result[WorkOrder]:
        """Ask the shop for the repair; the foreman puts it on a work order later. The product honours the key."""
        try:
            raw = self.http.json(agency, "POST", "/pending-repairs", headers={"Idempotency-Key": key},
                                 json={"asset": asset, "description": description, "requested_by": "system"})
        except ProductUnavailable as e:   # a retired asset is a refusal, not an outage (as for work orders, A87)
            if (e.note or "").startswith("http 409") and "retired" in (e.note or ""):
                raise HardLimit([f"asset_retired:{asset}"]) from e
            raise
        back = self.reader.pending_by_key(agency, key)   # read back by key (accepts a request already converted)
        if back.status != "ok" or back.value is None or back.value.get("id") != raw.get("id"):
            return Result.part(map_pending_repair(self.deps, agency, raw), "maintenance",
                               f"readback failed: {back.note or 'pending repair not found by key'}")
        return self._pending_result(agency, back.value)

    def _cancel_pending(self, agency: str, pr: dict) -> Result[WorkOrder]:
        """Compensation for a pending repair: withdraw an open request (or one attached to a work order someone
        else opened, which stays untouched); a request that created its own work order cancels that work order."""
        if pr.get("requested_by") != "system":
            why = f"cancel_scope: pending repair {pr['id']} was requested by a human; only system requests may go"
            raise HardLimit([why])
        if pr["status"] == "CANCELLED":
            return Result.success(self.reader.resolve_pending(agency, pr), note="already_cancelled")
        if (done := self._cancel_own_work_order(agency, pr)) is not None:
            return done
        try:
            self.http.json(agency, "POST", f"/pending-repairs/{pr['id']}/cancel")
        except ProductUnavailable as e:
            # bug sweep B (A154): read OPEN, then the foreman converted it before our cancel (409). Re-read: when it
            # now sits on the work order it created, cancel THAT one; any other 409 (or error) still surfaces
            if not (e.note or "").startswith("http 409"):
                raise
            now = self.reader._pr_raw(agency, pr["id"])
            if now is None or (done := self._cancel_own_work_order(agency, now)) is None:
                raise
            return done
        return self.reader.get_work_order(agency, pr["id"])

    def _cancel_own_work_order(self, agency: str, pr: dict) -> Result[WorkOrder] | None:
        """A system request the foreman put on a work order it CREATED: cancel that work order (idempotent: an
        already closed one is left as it is). None when the request is not on a work order of its own."""
        if pr.get("requested_by") != "system" or pr["status"] != "CONVERTED" or not pr.get("work_order"):
            return None
        wo = self.http.json(agency, "GET", f"/workorders/{pr['work_order']}")
        if wo.get("from_pending_repair") != pr["id"]:
            return None
        if wo.get("opened_by") != "system":   # the work-order cancel path's hard limit (only system-opened ones)
            raise HardLimit([f"cancel_scope: work order {wo['id']} was opened by a human; only system-created may be cancelled"])
        if str(wo["status"]).lower() != "closed":
            self.http.json(agency, "POST", f"/workorders/{wo['id']}/cancel")
        return self.reader.get_work_order(agency, pr["id"])

    def cancel_work_order(self, agency: str, wo_id: str, key: str) -> Result[WorkOrder]:
        def go() -> Result[WorkOrder]:
            if maintenance_mode(self.deps, agency) == "pending_repair" and \
                    (pr := self.reader._pr_raw(agency, wo_id)) is not None:
                return self._cancel_pending(agency, pr)
            cur = self.reader.get_work_order(agency, wo_id)
            if cur.status != "ok":
                return cur
            wo = cur.value
            assert wo is not None
            if wo.source != "system":
                raise HardLimit([f"cancel_scope: work order {wo_id} was opened by a human; only system-created may be cancelled"])
            if wo.status == "closed":
                return Result.success(wo, note="already_closed")
            self.http.json(agency, "POST", f"/workorders/{wo_id}/cancel")
            return self.reader.get_work_order(agency, wo_id)
        return self._run(agency, "cancel_work_order", key, go)


class SchedulingWriter(_Base):
    """adapter id: scheduling_mock_v1. No product idempotency (A2) -> local idem store."""
    product = "scheduling"

    def __init__(self, deps: Deps):
        super().__init__(deps)
        self.reader = SchedulingReader(deps)

    def _set_status(self, agency: str, vehicle_id: str, path: str, body: dict, want: str, key: str) -> Result[Vehicle]:
        native = self._native_vehicle(agency, vehicle_id, "scheduling")
        # changed_by marks the change as ours so the resulting VehicleStatusChanged is ignored (caused_by_us)
        self.http.request(agency, "POST", f"/vehicles/{native}/{path}",
                          json={**body, "changed_by": f"{SYSTEM_ACTOR}:{key}"})
        back = self.reader.get_vehicle(agency, vehicle_id)
        if back.status != "ok":
            return Result.part(None, "scheduling", f"write sent, readback failed: {back.note}")
        assert back.value is not None
        if back.value.status != want:
            return Result.part(back.value, "scheduling", f"readback_mismatch: status {back.value.status}, expected {want}")
        return back

    def set_vehicle_unavailable(self, agency: str, vehicle_id: str, start: datetime, end: datetime | None,
                                key: str) -> Result[Vehicle]:
        return self._run(agency, "set_vehicle_unavailable", key, lambda: self._set_status(
            agency, vehicle_id, "unavailable", {"start": _iso(start), "end": _iso(end) if end else None},
            "out_of_service", key))

    def set_vehicle_available(self, agency: str, vehicle_id: str, key: str) -> Result[Vehicle]:
        return self._run(agency, "set_vehicle_available", key,
                         lambda: self._set_status(agency, vehicle_id, "available", {}, "ready", key))

    def reassign_block(self, agency: str, block_id: str, to_vehicle_id: str, key: str, *,
                       compensation: bool = False) -> Result[Block]:
        def go() -> Result[Block]:
            native_to = self._native_vehicle(agency, to_vehicle_id, "scheduling")
            if not compensation:
                self._recheck(agency, block_id, to_vehicle_id)
            # A57: compensation is a restore of the prior assignment; the product accepts it even while that
            # vehicle is out of service (new assignments there stay refused).
            hdr = {"X-Restore": "true"} if compensation else {}
            self.http.request(agency, "POST", f"/blocks/{block_id}/reassign", json={"vehicle": native_to},
                              headers=hdr)
            back = self.reader.get_block(agency, block_id)
            if back.status != "ok":
                return Result.part(None, "scheduling", f"write sent, readback failed: {back.note}")
            assert back.value is not None
            if back.value.vehicle_id != to_vehicle_id:
                return Result.part(back.value, "scheduling", "readback_mismatch: block not on requested vehicle")
            return back
        return self._run(agency, "reassign_block", key, go)

    def _recheck(self, agency: str, block_id: str, to_vehicle_id: str) -> None:
        """Hard limits re-evaluated from live product state through the clearance module, regardless of what
        the caller claims. Only KNOWN violations block; unverifiable items were the approver's to acknowledge."""
        policy = self.deps.policy_for(agency)
        block = self.reader.get_block(agency, block_id)
        veh = self.reader.get_vehicle(agency, to_vehicle_id)
        ready = self.reader.list_ready_vehicles(agency, block.value.start, block.value.end, Needs()) \
            if block.status == "ok" else block
        sched = self.reader.list_blocks(agency, block.value.start, block.value.end) \
            if block.status == "ok" else block
        for r in (block, veh, ready, sched):
            if r.status != "ok":  # cannot verify -> cannot act (partial counts: conflicts unchecked)
                raise ProductUnavailable(r.product or "scheduling", f"cannot verify hard limits: {r.note}")
        assert block.value and veh.value and ready.value is not None and sched.value is not None
        # S2 fix round F4: the spare's maintenance asset status, read live (a KNOWN retired asset blocks; an
        # unknown one was the approver's to acknowledge)
        ready_ids = [x.id for x in ready.value]
        assets = MaintenanceReader(self.deps).asset_statuses(agency, [to_vehicle_id, *ready_ids]).value
        n_ready, n_unknown = count_ready(ready_ids, assets)   # R4-2: known-retired spares are not counted
        facts = Facts(blocks={block.value.id: block.value}, vehicles={veh.value.id: veh.value}, operators={},
                      ready_count=n_ready, commitments=commitments_from_blocks(sched.value),
                      profiles={"scheduling": self.reader.capability_profile(agency)}, asset_status=assets,
                      ready_unknown=n_unknown)
        c = safe_vehicle_move(block.value.id, veh.value.id, facts, policy)   # an exception blocks (fail closed)
        v = [i.detail or i.rule for i in c.violations if i.rule != "last_spare"]
        if to_vehicle_id not in {x.id for x in ready.value}:
            v.append(f"spare_not_ready: {to_vehicle_id} is not a ready, unassigned vehicle for this window")
        else:
            v += [i.detail for i in c.violations if i.rule == "last_spare"]
        if v:
            raise HardLimit(v)


class WorkforceWriter(_Base):
    """adapter id: workforce_mock_v1. No idempotency (A3) -> local idem store."""
    product = "workforce"

    def __init__(self, deps: Deps):
        super().__init__(deps)
        self.sched = SchedulingReader(deps)
        self.workforce = WorkforceReader(deps)

    def assign_operator(self, agency: str, block_id: str, operator_id: str, key: str, *,
                        compensation: bool = False) -> Result[Block]:
        def go() -> Result[Block]:
            native_op = native_of(self.deps, agency, operator_id, "workforce", operator=True)   # G3 (A147)
            if native_op is None:
                raise ProductUnavailable("crosswalk", f"no workforce id for {operator_id}")
            if not compensation:
                self._recheck(agency, block_id, operator_id)
            self.http.request(agency, "POST", f"/blocks/{block_id}/operator", json={"operator": native_op})
            back = self.sched.get_block(agency, block_id)
            if back.status != "ok":
                return Result.part(None, "scheduling", f"write sent, readback failed: {back.note}")
            assert back.value is not None
            if back.value.operator_id != operator_id:
                return Result.part(back.value, "scheduling", "readback_mismatch: operator not applied")
            return back
        return self._run(agency, "assign_operator", key, go)

    def _recheck(self, agency: str, block_id: str, operator_id: str) -> None:
        policy = self.deps.policy_for(agency)
        block = self.sched.get_block(agency, block_id)
        if block.status != "ok":
            raise ProductUnavailable(block.product or "scheduling", f"cannot verify hard limits: {block.note}")
        assert block.value is not None
        veh = self.sched.get_vehicle(agency, block.value.vehicle_id)   # the vehicle now on the block
        sched = self.sched.list_blocks(agency, block.value.start, block.value.end)
        for r in (veh, sched):
            if r.status != "ok":
                raise ProductUnavailable(r.product or "scheduling", f"cannot verify hard limits: {r.note}")
        assert veh.value is not None and sched.value is not None
        ops, profile = self.workforce._export(agency)
        op = next((o for o in ops if o.id == operator_id), None)
        if op is None:
            raise HardLimit([f"operator_unknown: {operator_id} not in workforce export"])
        facts = Facts(blocks={block.value.id: block.value}, vehicles={veh.value.id: veh.value},
                      operators={op.id: op}, ready_count=None, commitments=commitments_from_blocks(sched.value),
                      ready_unknown=0,   # R5-2: no spare count in an operator re-check (ready_count None)
                      profiles={"workforce": profile, "scheduling": self.sched.capability_profile(agency)})
        items = guarded(f"operator {op.id} for block {block.value.id}", lambda: [  # an exception blocks (F4)
            *operator_items(block.value, op, known_vehicle_type(veh.value, facts), facts, policy),   # R2-1
            double_booking_item(Option((Assign(block.value.id, veh.value.id, op.id),)), facts)])
        v = [i.detail or i.rule for i in items if i.status == "violation"]
        if v:
            raise HardLimit(v)


class AlertsWriter(_Base):
    """adapter id: alerts_mock_v1. Irreversible; no product idempotency -> local idem store."""
    product = "alerts"

    def publish_rider_alert(self, agency: str, block_id: str, message: str, key: str) -> Result[dict]:
        def go() -> Result[dict]:
            header = message.strip().split("\n")[0][:100]
            raw: Any = self.http.json(agency, "POST", "/alerts", json={
                "block": block_id, "header_text": header, "description_text": message})
            # Alerts are eventually visible (visible_after on sim clock): do not wait for read-back.
            return Result.success({"alert_id": raw["id"], "block_id": block_id}, note="eventually_visible")
        return self._run(agency, "publish_rider_alert", key, go)


__all__ = ["AlertsWriter", "MaintenanceWriter", "SchedulingWriter", "WorkforceWriter", "map_block"]
