"""Seeded stochastic world (Phase 10, Step 3.3): a generator that builds a `DayScript` of many days.

What is REAL and what is ASSUMED:
  REAL (derived from public NTD data, data/derived/fault_rates.yaml): the failure rate. Failures arrive as a Poisson
        process in revenue miles at rate 1 / (miles between major failures), the miles-between quantile being an
        explicit parameter. Default: the small-fleet (10-50 vehicles) bus cut, pooled 2022-24, p50 = 26,657 miles.
        For reference the all-agency bus quantiles (pooled 2022-24) are p10 5,699 / p50 18,576 / p90 112,305 miles.
  ASSUMED (everything else, tagged): revenue miles per vehicle-day (A93), the service day (A93), which system fails
        (config/fault_mix.yaml, A117/A118), SPN and FMI per system (fixtures/j1939_spn_fmi.yaml, A115/A116), the
        intermittent share and shape (A95), repair and inspection durations (config/shop_behaviour.yaml A119 + A96),
        call-out probability 0.07 per operator-shift with sensitivity 0.10 (A97; research context, NOT calibration),
        extra board 1 or 2 of every 10 operators (A98).

Determinism: every draw comes from a `random.Random` seeded with a string made of the seed and the entity it is for,
so the same seed gives a byte-identical timeline, the result does not depend on the order the vehicles or operators
are listed, and adding a vehicle does not reshuffle the others. No wall clock, no global random state.
"""
from __future__ import annotations

import json
import math
import random
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from transitos.data.loaders import QKEYS, load_fault_mix, load_fault_rates, load_j1939, load_shop_behaviour

from .script import Beat, DayScript, midnight

CALLOUT_P = 0.07                  # ASSUMED A97: probability an operator calls out of a shift (research context, not calibration)
CALLOUT_P_SENSITIVITY = 0.10      # ASSUMED A97: the sensitivity run
ASSUMED_REVENUE_MILES_PER_DAY = 150.0   # ASSUMED A93: a placeholder a caller may pass explicitly; never applied silently
P_INTERMITTENT = 0.30             # ASSUMED A95: share of SPN faults that clear and recur before the final persistent one
EXTRA_BOARD_PER_10 = (1, 2)       # ASSUMED A98: extra board is 1 or 2 of every 10 operators
FLEET_CUTS = {"small": "small_fleet_voms_10_50", "all": "all_agencies"}


@dataclass(frozen=True)
class StochasticConfig:
    seed: int
    agency: str
    vehicles: tuple[str, ...]
    operators: tuple[str, ...]
    start: datetime                                   # first day (read as 00:00 UTC of that date)
    days: int
    # Revenue miles per vehicle-day: a number for every vehicle, or a mapping that names EVERY vehicle. There is no
    # default: an unknown mileage is an error, never silently 0 miles (a known 0.0 in the mapping is allowed).
    revenue_miles_per_day: float | Mapping[str, float | None] | None
    mode: str = "MB"                                  # NTD mode: MB = bus
    quantile: str = "p50"
    fleet_cut: str = "small"                          # "small" (10-50 vehicle agencies) or "all"
    callout_p: float = CALLOUT_P
    service_start_h: float = 5.0                      # ASSUMED A93
    service_end_h: float = 21.0                       # ASSUMED A93
    p_intermittent: float = P_INTERMITTENT


@dataclass(frozen=True)
class StochasticWorld:
    script: DayScript
    extra_board: tuple[str, ...]
    meta: dict[str, Any]

    def to_json(self) -> str:
        return json.dumps({"script": self.script.to_dict(), "extra_board": list(self.extra_board), "meta": self.meta},
                          sort_keys=True, separators=(",", ":"))


def _miles(cfg: StochasticConfig) -> dict[str, float]:
    m = cfg.revenue_miles_per_day
    if m is None:
        raise ValueError("revenue_miles_per_day is required (unknown mileage is not zero miles)")
    out: dict[str, float] = {}
    for v in cfg.vehicles:
        x = m if not isinstance(m, Mapping) else m.get(v)
        if x is None:
            raise ValueError(f"no revenue mileage for {v!r}: unknown mileage is not zero miles")
        x = float(x)
        scalar_zero = not isinstance(m, Mapping) and x == 0
        if math.isnan(x) or math.isinf(x) or x < 0 or scalar_zero:
            raise ValueError(f"revenue miles per day for {v!r} must be a positive finite number, got {x!r}")
        out[v] = x
    return out


def _validate(cfg: StochasticConfig) -> None:
    if cfg.quantile not in QKEYS:
        raise ValueError(f"quantile must be one of {QKEYS}, got {cfg.quantile!r}")
    if cfg.fleet_cut not in FLEET_CUTS:
        raise ValueError(f"fleet_cut must be one of {tuple(FLEET_CUTS)}, got {cfg.fleet_cut!r}")
    if cfg.days < 1:
        raise ValueError("days must be >= 1")
    if not 0 <= cfg.callout_p <= 1:
        raise ValueError("callout_p must be a probability")
    if not 0 <= cfg.p_intermittent <= 1:
        raise ValueError("p_intermittent must be a probability")
    if not 0 <= cfg.service_start_h < cfg.service_end_h <= 24:
        raise ValueError("service hours must satisfy 0 <= start < end <= 24")
    if len(set(cfg.vehicles)) != len(cfg.vehicles) or len(set(cfg.operators)) != len(cfg.operators):
        raise ValueError("vehicle and operator ids must be unique")


def _rng(cfg: StochasticConfig, *parts: str) -> random.Random:
    return random.Random("|".join([str(cfg.seed), *parts]))


def _lognormal_h(rng: random.Random, spec) -> float:
    return rng.lognormvariate(math.log(spec.median), spec.sigma)


def _secs(td_hours: float) -> timedelta:
    return timedelta(seconds=round(td_hours * 3600))


def generate_world(cfg: StochasticConfig) -> StochasticWorld:
    _validate(cfg)
    miles = _miles(cfg)
    rates, mix, j1939, shop = load_fault_rates(), load_fault_mix(), load_j1939(), load_shop_behaviour()
    mbf = rates.miles_between(cfg.mode, cfg.quantile, cut=cfg.fleet_cut)   # REAL NTD-derived, pooled 2022-24
    systems = sorted(mix.systems)
    weights = [mix.systems[s].weight for s in systems]
    rows_by_system: dict[str, list] = {}
    for r in sorted(j1939.spn, key=lambda r: r.spn):
        rows_by_system.setdefault(r.system, []).append(r)
    day0 = midnight(cfg.start)
    span_h = cfg.service_end_h - cfg.service_start_h
    beats: list[Beat] = []
    arrivals = dropped = 0

    for veh in sorted(cfg.vehicles):
        mpd = miles[veh]
        if mpd == 0:
            continue                                   # a known zero: the bus did not run
        rng = _rng(cfg, "vehicle", veh)
        total, x, n, busy_until = mpd * cfg.days, 0.0, 0, day0
        while True:
            x += rng.expovariate(1.0 / mbf)            # Poisson process in revenue miles
            if x >= total:
                break
            arrivals += 1
            day_i, within = divmod(x, mpd)
            t0 = day0 + timedelta(days=int(day_i)) + _secs(cfg.service_start_h + span_h * within / mpd)
            system = rng.choices(systems, weights)[0]
            if t0 < busy_until:                        # a bus that is down accrues no revenue miles (A96): no new failure
                dropped += 1
                continue
            n += 1
            fid = f"{veh}-f{n}"
            rows = rows_by_system.get(system)
            if not rows:                               # no SPN for this system in the fixture: the driver reports it
                repair = _secs(_lognormal_h(rng, shop.repair_hours_by_severity["service_soon"]))
                beats.append(Beat(t0, veh, "dvir", {"fault_id": fid, "system": system,
                                                    "defect_text": f"{system.replace('_', ' ')} defect reported by operator"}))
                end = t0 + repair
            else:
                row = rng.choice(rows)
                fmi = rng.choice(row.typical_fmi)
                sev = j1939.severity(row.spn, fmi)
                base = {"fault_id": fid, "system": system, "spn": row.spn, "fmi": fmi, "severity": sev}
                lamps = {"stop": sev == "pull_vehicle", "warn": True}
                t = t0
                if rng.random() < cfg.p_intermittent:  # clears and recurs 1-2 times before the persistent final one
                    for _ in range(rng.randint(1, 2)):
                        on = timedelta(minutes=rng.randint(3, 20))
                        gap = timedelta(minutes=rng.randint(10, 90))
                        beats.append(Beat(t, veh, "fault_on", {**base, "lamps": lamps, "withdrawn": False,
                                                              "intermittent": True}))
                        beats.append(Beat(t + on, veh, "fault_off", {k: base[k] for k in ("fault_id", "spn", "fmi")}))
                        t = t + on + gap
                repair = _secs(_lognormal_h(rng, shop.repair_hours_by_severity[sev]))
                beats.append(Beat(t, veh, "fault_on", {**base, "lamps": lamps, "withdrawn": True,
                                                      "intermittent": False}))
                beats.append(Beat(t + repair, veh, "fault_off", {k: base[k] for k in ("fault_id", "spn", "fmi")}))
                end = t + repair
            rts = end + timedelta(minutes=rng.randint(15, 75))      # inspection before it goes back out (A96)
            beats.append(Beat(rts, veh, "return_to_service", {"fault_id": fid}))
            busy_until = rts

    # operators: the extra board covers; everyone else may call out of each day's shift
    ops = sorted(cfg.operators)
    extra: tuple[str, ...] = ()
    if ops:
        rb = _rng(cfg, "extra-board")
        size = max(1, round(len(ops) * rb.choice(EXTRA_BOARD_PER_10) / 10))
        extra = tuple(sorted(rb.sample(ops, min(size, len(ops)))))
    shifts = callouts = 0
    for op in ops:
        if op in extra:
            continue
        ro = _rng(cfg, "operator", op)
        for d in range(cfg.days):
            shifts += 1
            if ro.random() < cfg.callout_p:
                callouts += 1
                lead = timedelta(minutes=ro.randint(30, 180))
                beats.append(Beat(day0 + timedelta(days=d) + _secs(cfg.service_start_h) - lead, op, "callout",
                                  {"operator": op}))

    beats.sort(key=lambda b: b.at)                     # stable: ties keep construction order (sorted ids)
    meta = {
        "seed": cfg.seed, "mode": cfg.mode, "quantile": cfg.quantile, "fleet_cut": FLEET_CUTS[cfg.fleet_cut],
        "window": "pooled_2022_2024", "miles_between_major_failures": mbf,
        "rate_source": "REAL: NTD breakdowns 2022-24, data/derived/fault_rates.yaml (data.transportation.gov amkt-4ehs)",
        "other_numbers": "ASSUMED A93-A98 (docs/assumptions/phase10-step3.md); mix and fixtures ASSUMED A115-A119",
        "failures": arrivals, "dropped_while_out_of_service": dropped, "callouts": callouts, "operator_shifts": shifts,
        "callout_p": cfg.callout_p, "days": cfg.days, "vehicles": len(cfg.vehicles),
    }
    return StochasticWorld(DayScript(cfg.agency, tuple(beats)), extra, meta)
