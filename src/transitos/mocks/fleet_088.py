"""agency-088: a generated, seeded fleet whose identifiers disagree across the SIMULATED products (Phase 10 Step 2.2).

Drawn from data/derived/fleet_mix.yaml (vehicle type, seats, length, make, manufacture year; the model-year gap is
the fixture's own assumption, A120). Same seed, same `now` day -> byte-identical fleet.

Each vehicle carries its TRUTH (canonical fleet number, VIN, plate) plus what each product records about it, with
deliberate mess (A84):
  - scheduling (the agency's revenue roster, the canonical registry: A85): clean ids `BUS-0nn`, VIN, plate, name;
  - telematics: gateway serials as ids, a dirty fleet-number name (`bus 44`, `BUS-044`, `Bus O44`, ...), the VIN read
    from the engine (missing on older gateways), rarely a plate;
  - maintenance: unit numbers with inconsistent zero padding, a dirty name, VIN and plate typed by hand (one VIN
    with a typo that fails its check digit), and the BODY BUILDER as make for the cutaway whose VIN is the chassis
    maker's (49 CFR 565.13: the incomplete-vehicle manufacturer's VIN stays);
  - a device swap on a known date (old gateway removed, new one installed; both records stay in telematics);
  - a retired bus still present in maintenance (status `retired`; the product refuses new work orders on it);
  - a non-revenue service truck (telematics and maintenance only, never in the revenue roster);
  - an orphan telematics unit (no maintenance asset, no roster entry, no VIN).
`crosswalk_seed.crosswalk_entries("agency-088")` deliberately returns NO vehicle rows: the vehicle crosswalk is worked
out by `plumbing.discovery` from what the products serve. The `truth_*` keys exist only for tests.
"""
from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from typing import Any

from transitos.data import identifiers as idf
from transitos.data.loaders import load_fleet_mix

from .clock import iso

AGENCY = "agency-088"
SEED = 88088
_TYPES = {"Bus": "standard", "Cutaway": "cutaway", "Articulated Bus": "articulated"}
_VDS = {"standard": "B4D0F", "cutaway": "C2E1K", "articulated": "A6D0F"}   # fictitious descriptor sections (A75)
_YEARS = range(2010, 2026)                     # the generator's VIN year codes; older years are dropped (A86)
_ROLES = ["line"] * 9 + ["spare"] * 3
SWAP_IDX = 4                                   # bus whose telematics gateway is swapped
CUTAWAY_IDX = 5                                # forced cutaway on a Ford-style chassis
TYPO_VIN_IDX = 9                               # maintenance VIN typed with a wrong check digit
NO_ENGINE_VIN = (3, 7)                         # older gateways: no VIN from the engine


def _draw(rng: random.Random, shares: dict[str, float]) -> str:
    keys = sorted(k for k, v in shares.items() if v > 0 and not k.startswith("("))
    return rng.choices(keys, weights=[shares[k] for k in keys])[0]


def _gateway(rng: random.Random) -> str:
    return "GW-" + "".join(rng.choice("0123456789ABCDEFGHJKLMNPRSTUVWXYZ") for _ in range(6))


def _typo(vin: str) -> str:
    """Change one serial digit so the check digit no longer holds (a transcription error)."""
    d = vin[16]
    out = vin[:16] + ("0" if d != "0" else "1")
    return out if not idf.is_valid_vin(out) else vin[:15] + ("0" if vin[15] != "0" else "1") + vin[16]


def build_agency_088(now: datetime, realistic: bool = False) -> Any:
    """`realistic=True` (opt-in, never the default) puts the real chassis WMI from fixtures/realistic_wmis.yaml on the
    cutaway; the default uses only WMIs that vPIC did not recognise."""
    from .seed import (  # late import: seed.py registers this builder
        _OPS,
        Fleet,
        _training,
    )

    rng = random.Random(SEED)
    mix = load_fleet_mix()
    day = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    wmis = idf.synthetic_wmis()
    chassis_wmi = "1FD" if realistic else wmis[1]      # stand-in for the chassis maker's WMI
    type_shares = {k: mix.vehicle_type_share[k] for k in _TYPES}
    f = Fleet(AGENCY)

    for i, (hours_today, hours_week, a_from, a_to, extra, lift, quals) in enumerate(_OPS, start=1):
        f.operators.append({
            "idx": i, "canonical": f"op-{i}", "native": f"D{700 + i}", "qualifications": quals, "lift_trained": lift,
            "hours_today": hours_today, "hours_week": hours_week, "available_from": a_from, "available_to": a_to,
            "on_extra_board": extra, **_training(i, quals, lift, day),
        })

    def vehicle(idx: int, n: int, role: str) -> dict[str, Any]:
        ntd = "Cutaway" if idx == CUTAWAY_IDX else _draw(rng, type_shares)
        kind = _TYPES[ntd]
        tm = mix.by_type[ntd]
        seats = int(_draw(rng, tm.seating_capacity_share))
        length = float(_draw(rng, tm.length_share))
        make = _draw(rng, tm.make_share)
        years = {k: v for k, v in tm.manufacture_year_share.items() if k.isdigit() and int(k) in _YEARS}
        built = int(_draw(rng, years))
        gap = int(_draw(rng, {k: float(v) for k, v in mix.model_year_gap["model_year_minus_manufacture_year"].items()}))
        model_year = min(built + gap, max(_YEARS))
        body_wmis = [w for w in wmis if w != wmis[1]]
        wmi = chassis_wmi if idx == CUTAWAY_IDX else body_wmis[idx % len(body_wmis)]
        vin = idf.make_vin(wmi, _VDS[kind], model_year, "Z", 1000 + 37 * idx)
        plate = idf.make_plate(rng)
        clean = f"BUS-{n}"
        maint_id = rng.choice([str(n), f"{n:04d}", f"0{n}"])
        v: dict[str, Any] = {
            "idx": idx, "role": role, "type": kind, "seats": seats, "length_ft": length,
            "wheelchair_positions": 0 if idx == 12 else 2, "lift_ok": idx != 12,
            "depot": ("North", "South")[idx % 2], "status": "ready" if role == "spare" else "in_service",
            "make": make, "manufacture_year": built, "model_year": model_year,
            # truth (tests only)
            "truth_fleet_number": clean, "truth_vin": vin, "truth_plate": plate,
            # scheduling: the revenue roster
            "sched": f"BUS-{n:03d}", "name": f"Bus {n}", "sched_vin": vin, "sched_plate": plate,
            "sched_name": f"Bus {n}",
            # telematics
            "tele": _gateway(rng), "tele_name": idf.dirty_fleet_number(clean, rng),
            "tele_vin": None if idx in NO_ENGINE_VIN else vin,
            "tele_plate": idf.dirty_plate(plate, rng) if idx == 2 else None,
            # maintenance
            "maint": maint_id, "maint_name": idf.dirty_fleet_number(clean, rng),
            "maint_vin": _typo(vin) if idx == TYPO_VIN_IDX else idf.dirty_vin(vin, rng),
            "maint_plate": idf.dirty_plate(plate, rng), "maint_status": "active",
            "maint_make": make,
        }
        if idx == CUTAWAY_IDX:
            v["make"] = "Ford Motor Company (chassis)"      # what a VIN decode names: the chassis maker
            v["maint_make"] = make if "Ford" not in make else "Glaval Bus"   # the body builder (A84)
        if idx == SWAP_IDX:
            swap_at = day - timedelta(days=3) + timedelta(hours=10)
            v["tele_prev"] = {"id": _gateway(rng), "name": v["tele_name"], "vin": v["tele_vin"],
                              "removed_at": iso(swap_at)}
            v["tele_installed_at"] = iso(swap_at)
        return v

    for idx, role in enumerate(_ROLES, start=1):
        f.vehicles.append(vehicle(idx, 40 + idx, role))
    # retired bus: maintenance only, status retired (never in the roster, never a spare, never a fault source)
    retired = vehicle(13, 39, "retired")
    retired.update(sched=None, tele=None, maint_status="retired", status="retired")
    retired.pop("tele_prev", None)
    # non-revenue service truck: telematics and maintenance, never in the revenue roster
    truck = vehicle(14, 7, "non_revenue")
    truck.update(sched=None, tele_name="Service Truck 7", maint_name="SERVICE TRUCK 7", maint="ST-7",
                 status="non_revenue")
    # orphan telematics unit: no asset, no roster entry, no VIN
    orphan = vehicle(15, 0, "orphan")
    orphan.update(sched=None, maint=None, tele_name="Spare gateway (unassigned)", tele_vin=None, tele_plate=None,
                  status="orphan")
    f.vehicles += [retired, truck, orphan]

    for v in f.vehicles:
        if v["maint"] is None or v["maint_status"] == "retired":
            continue
        meter = 20000 + 1700 * v["idx"]
        f.pm.append({"vehicle_idx": v["idx"], "service_code": "PM-A", "due_mi": meter + 3000 + 250 * v["idx"],
                     "meter_mi": meter})

    ops = f.operators
    for v in f.vehicles:
        if v["role"] != "line":
            continue
        i = v["idx"]
        for k in range(8):
            start = day + timedelta(hours=5 + 2 * k)
            wc_need = v["wheelchair_positions"] > 0 and k % 3 == 0
            op = ops[(i - 1) % 10] if k < 4 else ops[(i + 4) % 10]
            f.blocks.append({
                "id": f"BLK-{i}-{k + 1}", "vehicle_idx": i, "vehicle": v["sched"], "operator": op["native"],
                "start": iso(start), "end": iso(start + timedelta(hours=2)), "start_depot": v["depot"],
                "needs": {"seats": min(v["seats"], 12 if v["type"] == "cutaway" else 20),
                          "wheelchair_positions": 1 if wc_need else 0, "lift": wc_need, "type": v["type"]},
            })
    return f
