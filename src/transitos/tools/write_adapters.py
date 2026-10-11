"""Write adapters. Every write: (1) kill-switch gate, (2) idempotent replay, (3) hard-limit re-check,
(4) product call, (5) read-back. Blocked writes return Result.blocked(...) (contracts/results.py:
status unavailable, product "policy", note "kill_switch: ..." | "hard_limit: ...").

Reassign/assign accept `compensation=True` (keyword, optional) to skip needs/spares/hours checks when
restoring the original assignment during a saga rollback. Everything else is checked."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from transitos.contracts.events import SYSTEM_ACTOR
from transitos.contracts.objects import Block, Needs, Vehicle, WorkOrder
from transitos.contracts.results import HARD_LIMIT, KILL_SWITCH, Result

from .common import Deps, Http, ProductUnavailable, native_of
from .policy import (
    check_lift_qualification,
    check_min_spares,
    check_operator_hours,
    check_spare_meets_needs,
    kill_switch,
)
from .read_adapters import (
    MaintenanceReader,
    SchedulingReader,
    WorkforceReader,
    _iso,
    map_block,
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
        if kill_switch(agency, self.deps.killswitch, policy):
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
            existing = self.reader.find_open_work_order(agency, vehicle_id)
            if existing.status == "unavailable":
                return existing  # type: ignore[return-value]
            if existing.value is not None:  # reuse (typically human-opened) WO; never duplicate
                return Result.success(existing.value, note="already_open")
            raw = self.http.json(agency, "POST", "/workorders", headers={"Idempotency-Key": key},
                                 json={"asset": asset, "description": description, "opened_by": "system"})
            back = self.reader.get_work_order(agency, str(raw["id"]))  # read back
            if back.status != "ok":
                return Result.part(map_work_order(self.deps, agency, raw), "maintenance",
                                   f"readback failed: {back.note}")
            return back
        return self._run(agency, "create_work_order", key, go, idem=False)

    def cancel_work_order(self, agency: str, wo_id: str, key: str) -> Result[WorkOrder]:
        def go() -> Result[WorkOrder]:
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
        """Hard limits re-evaluated from live product state, regardless of what the caller claims."""
        limits = self.deps.policy_for(agency).limits
        block = self.reader.get_block(agency, block_id)
        veh = self.reader.get_vehicle(agency, to_vehicle_id)
        ready = self.reader.list_ready_vehicles(agency, block.value.start, block.value.end, Needs()) \
            if block.status == "ok" else block
        for r in (block, veh, ready):
            if r.status != "ok":  # cannot verify -> cannot act (partial counts: conflicts unchecked)
                raise ProductUnavailable(r.product or "scheduling", f"cannot verify hard limits: {r.note}")
        assert block.value and veh.value and ready.value is not None
        v = check_spare_meets_needs(veh.value, block.value.needs)
        if veh.value.status != "ready" or to_vehicle_id not in {x.id for x in ready.value}:
            v.append(f"spare_not_ready: {to_vehicle_id} is not a ready, unassigned vehicle for this window")
        else:
            v += check_min_spares(len(ready.value), limits)
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
            native_op = native_of(self.deps, agency, operator_id, "workforce")
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
        limits = self.deps.policy_for(agency).limits
        block = self.sched.get_block(agency, block_id)
        if block.status != "ok":
            raise ProductUnavailable(block.product or "scheduling", f"cannot verify hard limits: {block.note}")
        assert block.value is not None
        op = next((o for o in self.workforce.operators(agency) if o.id == operator_id), None)
        if op is None:
            raise HardLimit([f"operator_unknown: {operator_id} not in workforce export"])
        v = check_operator_hours(op.hours_today, block.value.start, block.value.end, limits)
        v += check_lift_qualification(block.value.needs, op)
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
