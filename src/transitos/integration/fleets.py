"""Synthetic fleet for the verification-scenario baseline (transitos.verification.scenarios docstring),
in the mocks' Fleet shape so the REAL mock products serve it.

veh-1 (scheduling BUS-44 / telematics T-77 / maintenance 1106) in service on blk-1 (T0+1h..T0+5h),
operator OP-1; ready spares veh-2.. (BUS-45.., T-78.., 1107..); OP-1 assigned, OP-2 extra board.
Vehicle type is "standard" (the mocks rank cutaway < standard < articulated; F's FakeWorld says "bus").

Phase 10: operators carry the v1.2 qualification fields (licence class B, passenger endorsement, trained on
the block's vehicle type, every credential valid for a year) so the 24 original scenarios keep their meaning;
`operators_qualified=False` now means "trained on a different type" (the legacy string is kept too).
Step 1 scenarios (verification/scenarios_step1.py) subclass Tweaks; the extra knobs are read with getattr so
the original `Tweaks` works unchanged.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from transitos.mocks.clock import iso
from transitos.mocks.seed import Fleet
from transitos.verification.scenarios import AGENCY, Tweaks
from transitos.verification.scenarios_step2 import vehicle_identity

VEHICLE_TYPE = "standard"
BLOCK_ID = "blk-1"
LENGTH_FT = {"cutaway": 24.0, "standard": 40.0, "articulated": 60.0}   # ASSUMED A66 (as mocks/seed.py)


def scenario_fleet(t: Tweaks):
    g = lambda name, default: getattr(t, name, default)

    def build(now: datetime) -> Fleet:
        f = Fleet(AGENCY)
        spare_type, block_type = g("spare_type", VEHICLE_TYPE), g("block_type", VEHICLE_TYPE)

        def veh(n: int, role: str, seats: int, wc: int, lift: bool, status: str, vtype: str) -> dict:
            return {"idx": n, "canonical": f"veh-{n}", "role": role, "sched": f"BUS-{43 + n}",
                    "tele": f"T-{76 + n}", "maint": str(1105 + n), "name": f"Bus {43 + n}", "type": vtype,
                    "seats": seats, "wheelchair_positions": wc, "lift_ok": lift, "depot": "North", "status": status,
                    "length_ft": (g("spare_length_ft", None) if role == "spare" else None) or LENGTH_FT.get(vtype),
                    **vehicle_identity(n, t)}   # Phase 10 Step 2 knobs (none set: nothing added)

        f.vehicles.append(veh(1, "line", 40, 2, True, "in_service", block_type))
        for i in range(t.spares):
            f.vehicles.append(veh(2 + i, "spare", t.spare_seats, t.spare_wheelchair_positions, t.spare_lift_ok,
                                  "ready", spare_type))
        for v in f.vehicles:  # PM well inside its interval: the mock itself never raises PMOverdue here
            f.pm.append({"vehicle_idx": v["idx"], "service_code": "PM-A", "due_mi": 10000, "meter_mi": 9000})
        quals = VEHICLE_TYPE if t.operators_qualified else "van"
        trained = list(g("operator_trained_types", (VEHICLE_TYPE,))) if t.operators_qualified else ["van"]
        endorsements = list(g("operator_endorsements", ("P",)))
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        offsets = dict(g("credential_days", ()))          # code -> days from the scenario day (0 = expires today)
        creds = [*endorsements, *trained, *(["lift"] if t.operators_lift_trained else [])]
        valid = ";".join(f"{c}={iso(day + timedelta(days=offsets.get(c, 365)))}" for c in creds)
        assigned_h = t.operator_hours_today if t.assigned_operator_hours_today is None else t.assigned_operator_hours_today
        for i, (hours, extra) in enumerate(((assigned_h, False), (t.operator_hours_today, True)), start=1):
            f.operators.append({"idx": i, "canonical": f"op-{i}", "native": f"OP-{i}", "qualifications": quals,
                                "lift_trained": t.operators_lift_trained, "hours_today": hours,
                                "hours_week": g("operator_hours_week", 20.0),
                                "available_from": "05:00", "available_to": "21:00", "on_extra_board": extra,
                                "licence_class": g("operator_licence_class", "B"),
                                "endorsements": ";".join(endorsements), "trained_vehicle_types": ";".join(trained),
                                "training_valid_to": valid, "medical_valid_to": iso(day + timedelta(days=365)),
                                "last_duty_end": None, "duty_start_today": None})
        start = now + timedelta(hours=1)
        needs = {"seats": 30, "wheelchair_positions": t.block_wheelchair_positions, "lift": t.block_needs_lift,
                 "type": block_type}
        if (limit := g("block_max_length_ft", None)) is not None:
            needs["max_vehicle_length_ft"] = limit
        f.blocks.append({"id": BLOCK_ID, "vehicle_idx": 1, "vehicle": "BUS-44", "operator": "OP-1",
                         "start": iso(start), "end": iso(start + timedelta(hours=4)), "start_depot": "North",
                         "needs": needs})
        if g("second_block", False):      # a second block on the faulty vehicle (spare reuse, or overlap)
            s2 = now + timedelta(hours=g("second_block_start_h", 6.0))
            f.blocks.append({"id": "blk-2", "vehicle_idx": 1, "vehicle": "BUS-44", "operator": "OP-2",
                             "start": iso(s2), "end": iso(s2 + timedelta(hours=2)), "start_depot": "North",
                             "needs": {**needs, "seats": 20, "wheelchair_positions": 0, "lift": False}})
        return f
    return build
