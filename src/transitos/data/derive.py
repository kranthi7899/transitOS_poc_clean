"""P4: derive the small committed artifacts in data/derived/ from data/reference.db.

Every output file starts with a provenance header comment (source dataset, vintage, query, row counts, date).
Quantiles use linear interpolation between order statistics (the numpy default). Nothing here reads the network.

If the NTD breakdowns table is missing, `assumed_fault_rates` writes hand-set values tagged `source: assumption`
and the file header says so (owner decision, amendment A Q4).
"""
from __future__ import annotations

import math
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

import yaml

QUANTILES = (("p10", 0.10), ("p25", 0.25), ("p50", 0.50), ("p75", 0.75), ("p90", 0.90))
BREAKDOWN_TABLE = "ref_ntd_breakdowns_2022_2024"
RVI_TABLE = "ref_ntd_rvi_2024"
WO_TABLE = "ref_cincinnati_closed_2022_2024"

BREAKDOWN_SQL = (
    f"select report_year, agency_voms, mode, mode_name, major_mechanical_failures, major_mechanical_failures_1, "
    f"vehicle_passenger_car_revenue, vehicle_passenger_car_miles_2 from {BREAKDOWN_TABLE} where mode = ?"
)


def percentile(values: list[float], q: float) -> float:
    if not values:
        raise ValueError("percentile of an empty list")
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    pos = q * (len(xs) - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def _num(v) -> float | None:
    if v is None:
        return None
    s = str(v).strip()
    if s == "":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def write_yaml(path: Path, body: dict, header: list[str]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    head = "".join(f"# {line}\n" for line in header)
    path.write_text(head + yaml.safe_dump(body, sort_keys=False, default_flow_style=False, width=110))


def _has_table(con: sqlite3.Connection, name: str) -> bool:
    return con.execute("select 1 from sqlite_master where type='table' and name=?", (name,)).fetchone() is not None


# --------------------------------------------------------------------------------------------- fault rates
def _dist(miles: list[float]) -> dict:
    return {"n": len(miles), "miles_between_major_failures": {k: round(percentile(miles, q)) for k, q in QUANTILES}}


def derive_fault_rates(con: sqlite3.Connection, modes=("MB", "DR"), min_cut_rows: int = 30,
                       today: str | None = None) -> dict:
    if not _has_table(con, BREAKDOWN_TABLE):
        raise LookupError(f"{BREAKDOWN_TABLE} not found in reference db (run make data)")
    out: dict = {
        "source": "NTD breakdowns",
        "source_dataset": "ntd_breakdowns_2022_2024 (data.transportation.gov amkt-4ehs)",
        "derived_on": today or date.today().isoformat(),
        "definition": "miles between major failures = revenue miles / major mechanical failures, per "
                      "agency-mode-type-of-service-year row",
        "poisson_rule": "model failures as a Poisson process in revenue miles with rate 1 / chosen quantile",
        "row_filter": "failures > 0, revenue miles > 0, and neither the failures flag nor the revenue-miles flag "
                      "(Q or W) set",
        "modes": {},
        "exclusions": {},
    }
    for mode in modes:
        rows = con.execute(BREAKDOWN_SQL, (mode,)).fetchall()
        excl = {"no_major_failures": 0, "no_revenue_miles": 0, "questionable_failures": 0,
                "questionable_revenue_miles": 0}
        kept: list[tuple[str, float, float]] = []  # (year, voms, miles per failure)
        name = rows[0][3] if rows else mode
        for year, voms, _m, _n, fail, fail_flag, rev, rev_flag in rows:
            f, r = _num(fail), _num(rev)
            if f is None or f <= 0:
                excl["no_major_failures"] += 1
            elif r is None or r <= 0:
                excl["no_revenue_miles"] += 1
            elif (fail_flag or "").strip():
                excl["questionable_failures"] += 1
            elif (rev_flag or "").strip():
                excl["questionable_revenue_miles"] += 1
            else:
                kept.append((str(year), _num(voms) or 0.0, r / f))
        out["exclusions"][mode] = excl

        def cut(sel, kept=kept):
            y24 = [m for y, v, m in kept if y == "2024" and sel(v)]
            pooled = [m for y, v, m in kept if sel(v)]
            return {"y2024": _dist(y24) if y24 else None,
                    "pooled_2022_2024": _dist(pooled) if pooled else None}

        mode_out = {"name": name, "rows_total": len(rows), "rows_kept": len(kept), "all_agencies": cut(lambda v: True)}
        small = cut(lambda v: 10 <= v <= 50)
        pooled_n = small["pooled_2022_2024"]["n"] if small["pooled_2022_2024"] else 0
        if pooled_n < min_cut_rows:
            small = {"y2024": None, "pooled_2022_2024": None, "note": f"too few rows ({pooled_n} < {min_cut_rows})"}
        else:
            small["note"] = "agency_voms between 10 and 50 inclusive"
        mode_out["small_fleet_voms_10_50"] = small
        out["modes"][mode] = mode_out
    return out


def assumed_fault_rates(today: str | None = None) -> dict:
    """Fallback ONLY when the NTD breakdowns could not be fetched. Hand-set, not data. Tagged everywhere."""
    def d(p10, p25, p50, p75, p90):
        return {"n": None, "miles_between_major_failures": {"p10": p10, "p25": p25, "p50": p50, "p75": p75, "p90": p90},
                "source": "assumption"}
    return {
        "source": "assumption",
        "derived_on": today or date.today().isoformat(),
        "note": "NTD breakdowns could not be fetched; these are hand-set placeholders (owner decision), not data.",
        "modes": {
            "MB": {"name": "Bus", "source": "assumption", "all_agencies": {"y2024": d(4000, 7000, 12000, 20000, 40000),
                   "pooled_2022_2024": d(4000, 7000, 12000, 20000, 40000)},
                   "small_fleet_voms_10_50": {"y2024": None, "pooled_2022_2024": None, "note": "assumed: no cut"}},
            "DR": {"name": "Demand Response", "source": "assumption",
                   "all_agencies": {"y2024": d(6000, 10000, 18000, 30000, 60000),
                                    "pooled_2022_2024": d(6000, 10000, 18000, 30000, 60000)},
                   "small_fleet_voms_10_50": {"y2024": None, "pooled_2022_2024": None, "note": "assumed: no cut"}},
        },
        "exclusions": {},
    }


# ----------------------------------------------------------------------------------------------- fleet mix
def _share(counter: dict, top_n: int, total: float) -> dict:
    items = sorted(counter.items(), key=lambda kv: (-kv[1], str(kv[0])))
    head = items[:top_n]
    rest = sum(v for _, v in items[top_n:])
    out = {str(k): round(v / total, 4) for k, v in head}
    if rest:
        out["(other)"] = round(rest / total, 4)
    return out


def derive_fleet_mix(con: sqlite3.Connection, today: str | None = None, top_n: int = 10,
                     types=("Bus", "Cutaway", "Articulated Bus")) -> dict:
    if not _has_table(con, RVI_TABLE):
        raise LookupError(f"{RVI_TABLE} not found in reference db (run make data)")
    rows = con.execute(
        f'select "Vehicle Type","Manufacturer","Model","Manufacture Year","Total Fleet Vehicles","ADA Fleet Vehicles",'
        f'"Fuel Type","Vehicle Length","Seating Capacity" from {RVI_TABLE}').fetchall()
    by_type_total: dict[str, float] = defaultdict(float)
    for r in rows:
        by_type_total[r[0]] += _num(r[4]) or 0
    grand = sum(by_type_total.values())
    out: dict = {
        "source": "NTD 2024 Revenue Vehicle Inventory",
        "source_dataset": "ntd_rvi_2024",
        "derived_on": today or date.today().isoformat(),
        "weighting": "shares are weighted by Total Fleet Vehicles (rows are fleets, not vehicles)",
        "rows_total": len(rows),
        "vehicles_total": int(grand),
        "vehicle_type_share": {k: round(v / grand, 4) for k, v in sorted(by_type_total.items(), key=lambda kv: -kv[1])},
        "by_type": {},
        "model_year_gap": {
            "source": "assumption",
            "id": "A120",
            "model_year_minus_manufacture_year": {"0": 0.7, "1": 0.3},
            "note": "The inventory has a 'Manufacture Year' and a 'Rebuild Year' column but NO model-year column, so "
                    "the gap cannot be measured from it. The NTD glossary says a model-2013 vehicle was likely made "
                    "in 2012; the 0.7/0.3 split is an assumption.",
        },
    }
    for t in types:
        sub = [r for r in rows if r[0] == t]
        if not sub:
            continue
        total = sum(_num(r[4]) or 0 for r in sub)

        def agg(idx, blank="(blank)", sub=sub):
            c: dict = defaultdict(float)
            for r in sub:
                v = r[idx]
                c[blank if v is None or str(v).strip() == "" else (int(v) if isinstance(v, float) and v.is_integer() else v)] += _num(r[4]) or 0
            return c

        years: dict = defaultdict(float)
        ytot = 0.0
        for r in sub:
            y = _num(r[3])
            if y is not None and 1990 <= y <= 2024:
                years[str(int(y))] += _num(r[4]) or 0
                ytot += _num(r[4]) or 0
        ada = sum(_num(r[5]) or 0 for r in sub)
        out["by_type"][t] = {
            "vehicles": int(total),
            "fleet_rows": len(sub),
            "make_share": _share(agg(1), top_n, total),
            "model_share": _share(agg(2), top_n, total),
            "seating_capacity_share": _share(agg(8), top_n, total),
            "length_share": _share(agg(7), top_n, total),
            "length_note": "unit not stated in the file; values such as 40 suggest feet (unverified)",
            "name_note": "make and model strings are as reported by agencies (case and spelling variants are not merged)",
            "fuel_share": _share(agg(6), top_n, total),
            "ada_share": round(min(ada / total, 1.0), 4) if total else None,
            "manufacture_year_share": {k: round(v / ytot, 4) for k, v in sorted(years.items())} if ytot else {},
            "manufacture_year_note": "years 1990-2024 only (the inventory contains implausible earlier years)",
        }
    return out


# ------------------------------------------------------------------------------------- repair durations
def _day(ts) -> date | None:
    if not ts:
        return None
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(ts))
    return date(int(m[1]), int(m[2]), int(m[3])) if m else None


def _dur_summary(days: list[float]) -> dict | None:
    if not days:
        return None
    pos = [math.log(x) for x in days if x > 0]
    out = {k: round(percentile(days, q), 2) for k, q in QUANTILES}
    out["share_zero_days"] = round(sum(1 for x in days if x == 0) / len(days), 4)
    return out, pos


def derive_repair_durations(con: sqlite3.Connection, today: str | None = None, min_rows: int = 100) -> dict:
    if not _has_table(con, WO_TABLE):
        raise LookupError(f"{WO_TABLE} not found in reference db")
    rows = con.execute(
        f"select job_type, datetime_open, datetime_first_labor, datetime_finished, datetime_closed, delay_hours, "
        f"reas_for_repair_desc from {WO_TABLE}").fetchall()
    groups: dict[str, list] = defaultdict(list)
    dropped = 0
    for jt, o, fl, fin, cl, delay, reason in rows:
        do, dfl, dfin, dcl = _day(o), _day(fl), _day(fin), _day(cl)
        if do is None or dcl is None or dcl < do:
            dropped += 1
            continue
        rec = {"o2c": (dcl - do).days,
               "o2fl": (dfl - do).days if dfl and dfl >= do else None,
               "fl2f": (dfin - dfl).days if dfl and dfin and dfin >= dfl else None,
               "delay": _num(delay)}
        groups[jt].append(rec)
        if jt == "REPAIR" and "ROAD CALL" in (reason or "").upper():
            groups["REPAIR_ROAD_CALL"].append(rec)

    def summarise(recs):
        if len(recs) < min_rows:
            return None
        o2c = [r["o2c"] for r in recs]
        s, logs = _dur_summary(o2c)
        mu = sum(logs) / len(logs) if logs else 0.0
        var = sum((x - mu) ** 2 for x in logs) / len(logs) if logs else 0.0
        delays = [r["delay"] for r in recs if r["delay"] is not None]
        pos_delays = [x for x in delays if x > 0]
        res = {
            "n": len(recs),
            "open_to_closed_days": s,
            "lognormal_fit": {"on": "open_to_closed_days > 0", "n_positive": len(logs), "mu": round(mu, 4),
                              "sigma": round(math.sqrt(var), 4)},
            "open_to_first_labor_days": (_dur_summary([r["o2fl"] for r in recs if r["o2fl"] is not None]) or [None])[0],
            "first_labor_to_finished_days": (_dur_summary([r["fl2f"] for r in recs if r["fl2f"] is not None]) or [None])[0],
            "delay_hours": {"share_positive": round(len(pos_delays) / len(delays), 4) if delays else None,
                            **({k: round(percentile(pos_delays, q), 2) for k, q in QUANTILES} if pos_delays else {})},
        }
        return res

    return {
        "source": "Cincinnati Fleet Preventative Maintenance & Repair Work Orders (2a8x-bxjm), closed, opened 2022-2024",
        "source_dataset": "cincinnati_closed_2022_2024",
        "licence": "PUBLIC_DOMAIN (Socrata licenseId, observed)",
        "derived_on": today or date.today().isoformat(),
        "rows_total": len(rows),
        "rows_dropped_bad_dates": dropped,
        "resolution_note": "timestamps are whole days (all at midnight), so durations are in whole days; a general "
                           "city fleet, not transit buses: use the SHAPE (skew, tail), not the absolute values",
        "by_job_type": {jt: summarise(groups.get(jt, [])) for jt in ("REPAIR", "PM")},
        "road_call_repairs": summarise(groups.get("REPAIR_ROAD_CALL", [])),
    }


# ----------------------------------------------------------------------------------------------------- main
def main() -> int:
    root = Path.cwd()
    db = root / "data" / "reference.db"
    out = root / "data" / "derived"
    con = sqlite3.connect(db)
    today = date.today().isoformat()

    def count(t):
        return con.execute(f"select count(*) from {t}").fetchone()[0] if _has_table(con, t) else 0

    try:
        fr = derive_fault_rates(con, today=today)
        hdr = ["Derived by transitos.data.derive. Source dataset: ntd_breakdowns_2022_2024 (NTD 2022-2024 Breakdowns,",
               f"data.transportation.gov amkt-4ehs, report years 2022-2024). Rows read: {count(BREAKDOWN_TABLE)}.",
               f"Query: {BREAKDOWN_SQL}",
               "Filter: failures > 0, revenue miles > 0, failures flag and revenue-miles flag both empty (A121). Fleet-level rows (A122).",
               f"Date derived: {today}. Public domain (Socrata licenseId USGOV_WORKS, observed)."]
    except LookupError:
        fr = assumed_fault_rates(today)
        hdr = ["ASSUMED VALUES, NOT DATA: the NTD breakdowns could not be fetched. Hand-set placeholders tagged",
               f"source: assumption (owner decision, amendment A Q4). Date: {today}."]
        print("WARNING: NTD breakdowns missing; wrote assumed fault rates", file=sys.stderr)
    write_yaml(out / "fault_rates.yaml", fr, hdr)

    fm = derive_fleet_mix(con, today=today)
    write_yaml(out / "fleet_mix.yaml", fm, [
        "Derived by transitos.data.derive. Source dataset: ntd_rvi_2024 (NTD 2024 Annual Database Revenue Vehicle",
        f"Inventory, xlsx, one sheet). Rows read: {count(RVI_TABLE)} fleet rows. Shares weighted by Total Fleet Vehicles.",
        ('Query: select "Vehicle Type","Manufacturer","Model","Manufacture Year","Total Fleet Vehicles",'
         '"ADA Fleet Vehicles","Fuel Type","Vehicle Length","Seating Capacity" from ref_ntd_rvi_2024'),
        f"Date derived: {today}. No VINs or plates exist in this dataset (rows are fleets).",
        "The model-year gap is NOT in the data: it is an assumption (A120). Blank/spelling/length-unit caveats: A123."])

    if _has_table(con, WO_TABLE):
        rd = derive_repair_durations(con, today=today)
        write_yaml(out / "repair_durations.yaml", rd, [
            "Derived by transitos.data.derive. Source dataset: cincinnati_closed_2022_2024 (City of Cincinnati Fleet",
            f"Preventative Maintenance & Repair Work Orders, 2a8x-bxjm). Rows read: {count(WO_TABLE)}. Licence: Socrata",
            "licenseId PUBLIC_DOMAIN (observed in the dataset metadata). General city fleet: shape only (A124). Whole-day resolution.",
            "Query: closed work orders with datetime_open in 2022-2024 (see data/EXTRA_SOURCES.yaml).",
            f"Date derived: {today}."])
    print("wrote", ", ".join(sorted(p.name for p in out.glob("*.yaml"))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
