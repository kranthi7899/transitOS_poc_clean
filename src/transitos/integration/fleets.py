"""Synthetic fleet for the verification-scenario baseline (transitos.verification.scenarios docstring),
in the mocks' Fleet shape so the REAL mock products serve it.

veh-1 (scheduling BUS-44 / telematics T-77 / maintenance 1106) in service on blk-1 (T0+1h..T0+5h),
operator OP-1; ready spares veh-2.. (BUS-45.., T-78.., 1107..); OP-1 assigned, OP-2 extra board.
Vehicle type is "standard" (the mocks rank cutaway < standard < articulated; F's FakeWorld says "bus").
"""
from __future__ import annotations

from datetime import datetime, timedelta

from transitos.mocks.clock import iso
from transitos.mocks.seed import Fleet
from transitos.verification.scenarios import AGENCY, Tweaks

VEHICLE_TYPE = "standard"
BLOCK_ID = "blk-1"


def scenario_fleet(t: Tweaks):
    def build(now: datetime) -> Fleet:
        f = Fleet(AGENCY)

        def veh(n: int, role: str, seats: int, wc: int, lift: bool, status: str) -> dict:
            return {"idx": n, "canonical": f"veh-{n}", "role": role, "sched": f"BUS-{43 + n}",
                    "tele": f"T-{76 + n}", "maint": str(1105 + n), "name": f"Bus {43 + n}", "type": VEHICLE_TYPE,
                    "seats": seats, "wheelchair_positions": wc, "lift_ok": lift, "depot": "North", "status": status}

        f.vehicles.append(veh(1, "line", 40, 2, True, "in_service"))
        for i in range(t.spares):
            f.vehicles.append(veh(2 + i, "spare", t.spare_seats, t.spare_wheelchair_positions, t.spare_lift_ok, "ready"))
        for v in f.vehicles:  # PM well inside its interval: the mock itself never raises PMOverdue here
            f.pm.append({"vehicle_idx": v["idx"], "service_code": "PM-A", "due_mi": 10000, "meter_mi": 9000})
        quals = VEHICLE_TYPE if t.operators_qualified else "van"
        assigned_h = t.operator_hours_today if t.assigned_operator_hours_today is None else t.assigned_operator_hours_today
        for i, (hours, extra) in enumerate(((assigned_h, False), (t.operator_hours_today, True)), start=1):
            f.operators.append({"idx": i, "canonical": f"op-{i}", "native": f"OP-{i}", "qualifications": quals,
                                "lift_trained": t.operators_lift_trained, "hours_today": hours, "hours_week": 20.0,
                                "available_from": "05:00", "available_to": "21:00", "on_extra_board": extra})
        start = now + timedelta(hours=1)
        f.blocks.append({"id": BLOCK_ID, "vehicle_idx": 1, "vehicle": "BUS-44", "operator": "OP-1",
                         "start": iso(start), "end": iso(start + timedelta(hours=4)), "start_depot": "North",
                         "needs": {"seats": 30, "wheelchair_positions": t.block_wheelchair_positions,
                                   "lift": t.block_needs_lift, "type": VEHICLE_TYPE}})
        return f
    return build
