"""Derive stage tests on tiny hand-made reference tables (no network, no real data)."""
from __future__ import annotations

import sqlite3

import pytest
import yaml

from transitos.data.derive import (
    assumed_fault_rates,
    derive_fault_rates,
    derive_fleet_mix,
    derive_repair_durations,
    percentile,
    write_yaml,
)


def test_percentile_linear_interpolation():
    xs = [1, 2, 3, 4, 5]
    assert percentile(xs, 0.5) == 3
    assert percentile(xs, 0.0) == 1 and percentile(xs, 1.0) == 5
    assert percentile(xs, 0.25) == 2
    assert percentile([10, 20], 0.5) == 15
    with pytest.raises(ValueError):
        percentile([], 0.5)


BREAKDOWN_COLS = ("agency,ntd_id,report_year,agency_voms,mode,mode_name,type_of_service,mode_voms,"
                  "major_mechanical_failures,major_mechanical_failures_1,vehicle_passenger_car_revenue,"
                  "vehicle_passenger_car_miles_2")


def _bd_db():
    con = sqlite3.connect(":memory:")
    con.execute(f"create table ref_ntd_breakdowns_2022_2024 ({BREAKDOWN_COLS})")
    rows = [
        # agency, ntd, year, voms, mode, name, tos, mvoms, failures, f_flag, revmiles, m_flag
        ("A", "1", "2024", "20", "MB", "Bus", "DO", "20", "10", "", "100000", ""),      # 10000 mi, small fleet
        ("B", "2", "2024", "200", "MB", "Bus", "DO", "200", "20", "", "100000", ""),    # 5000
        ("C", "3", "2023", "30", "MB", "Bus", "DO", "30", "5", "", "100000", ""),       # 20000, small fleet
        ("D", "4", "2024", "30", "MB", "Bus", "DO", "30", "0", "", "100000", ""),       # excluded: no failures
        ("E", "5", "2024", "30", "MB", "Bus", "DO", "30", "5", "", "0", ""),            # excluded: no miles
        ("F", "6", "2024", "30", "MB", "Bus", "DO", "30", "5", "Q", "100000", ""),      # excluded: failures flag
        ("G", "7", "2024", "30", "MB", "Bus", "DO", "30", "5", "", "100000", "W"),      # excluded: miles flag
        ("H", "8", "2024", "30", "DR", "Demand Response", "DO", "30", "10", "", "50000", ""),  # 5000 DR
    ]
    con.executemany(f"insert into ref_ntd_breakdowns_2022_2024 values ({','.join('?' * 12)})", rows)
    return con


def test_fault_rates_filters_and_quantiles():
    out = derive_fault_rates(_bd_db(), modes=("MB", "DR"), min_cut_rows=2, today="2026-10-11")
    mb = out["modes"]["MB"]
    assert mb["name"] == "Bus"
    assert mb["all_agencies"]["y2024"]["n"] == 2  # A and B survive in 2024
    assert mb["all_agencies"]["y2024"]["miles_between_major_failures"]["p50"] == 7500
    assert mb["all_agencies"]["pooled_2022_2024"]["n"] == 3
    assert mb["all_agencies"]["pooled_2022_2024"]["miles_between_major_failures"]["p50"] == 10000
    # small fleet cut agency_voms 10..50: A and C
    assert mb["small_fleet_voms_10_50"]["pooled_2022_2024"]["n"] == 2
    assert out["exclusions"]["MB"] == {"no_major_failures": 1, "no_revenue_miles": 1,
                                       "questionable_failures": 1, "questionable_revenue_miles": 1}
    assert out["modes"]["DR"]["all_agencies"]["y2024"]["miles_between_major_failures"]["p50"] == 5000
    assert out["source"] == "NTD breakdowns"
    assert "2,700" not in yaml.safe_dump(out)


def test_small_fleet_cut_omitted_when_too_few_rows():
    out = derive_fault_rates(_bd_db(), modes=("MB",), min_cut_rows=30, today="2026-10-11")
    assert out["modes"]["MB"]["small_fleet_voms_10_50"]["pooled_2022_2024"] is None
    assert "too few" in out["modes"]["MB"]["small_fleet_voms_10_50"]["note"]


def test_missing_table_raises_so_caller_can_fall_back():
    with pytest.raises(LookupError):
        derive_fault_rates(sqlite3.connect(":memory:"), modes=("MB",), today="2026-10-11")


def test_assumed_fallback_is_tagged_everywhere():
    out = assumed_fault_rates(today="2026-10-11")
    assert out["source"] == "assumption"
    for m in out["modes"].values():
        assert m["source"] == "assumption"
    path_text = yaml.safe_dump(out)
    assert "assumption" in path_text


def test_write_yaml_header_comment(tmp_path):
    p = tmp_path / "d" / "x.yaml"
    write_yaml(p, {"a": 1}, header=["Source: s", "Query: q"])
    text = p.read_text()
    assert text.startswith("# Source: s\n# Query: q\n")
    assert yaml.safe_load(text) == {"a": 1}


RVI_COLS = ['Vehicle Type', 'Manufacturer', 'Model', 'Manufacture Year', 'Total Fleet Vehicles', 'ADA Fleet Vehicles',
            'Active Fleet Vehicles', 'Fuel Type', 'Vehicle Length', 'Seating Capacity']


def _rvi_db():
    con = sqlite3.connect(":memory:")
    cols = ", ".join(f'"{c}"' for c in RVI_COLS)
    con.execute(f"create table ref_ntd_rvi_2024 ({cols})")
    rows = [
        ("Bus", "Gillig", "Low Floor", 2019, 30, 30, 28, "Diesel Fuel", 40, 38),
        ("Bus", "New Flyer", "XD40", 2020, 10, 10, 10, "Hybrid Diesel", 40, 40),
        ("Cutaway", "Starcraft", "Allstar", 2018, 40, 38, 40, "Gasoline", 25, 14),
        ("Cutaway", None, None, 2018, 20, 20, 20, "Gasoline", None, 12),
        ("Van", "Ford", "Transit", 2017, 5, 5, 5, "Gasoline", 20, 8),
        ("Bus", "Gillig", "Low Floor", 1870, 1, 1, 1, "Diesel Fuel", 40, 38),  # implausible year, dropped from year stats
    ]
    con.executemany(f"insert into ref_ntd_rvi_2024 values ({','.join('?' * 10)})", rows)
    return con


def test_fleet_mix_weighted_by_vehicle_count():
    out = derive_fleet_mix(_rvi_db(), today="2026-10-11", top_n=5)
    shares = out["vehicle_type_share"]
    total = 30 + 10 + 40 + 20 + 5 + 1
    assert shares["Bus"] == pytest.approx(41 / total, abs=1e-4)
    bus = out["by_type"]["Bus"]
    assert bus["make_share"]["Gillig"] == pytest.approx(31 / 41, abs=1e-4)
    assert out["by_type"]["Cutaway"]["make_share"]["(blank)"] == pytest.approx(20 / 60, abs=1e-4)
    assert bus["ada_share"] == pytest.approx(41 / 41, abs=1e-4)
    assert bus["manufacture_year_share"]["2019"] == pytest.approx(30 / 40, abs=1e-4)  # 1870 excluded
    assert out["model_year_gap"]["source"] == "assumption"
    assert "Manufacture Year" in out["model_year_gap"]["note"]


def _wo_db():
    con = sqlite3.connect(":memory:")
    con.execute("create table ref_cincinnati_closed_2022_2024 (job_type, work_order_status, datetime_open, "
                "datetime_first_labor, datetime_finished, datetime_closed, delay_hours, labor_hours, "
                "downtime_hrs_shop, reas_for_repair_desc)")
    rows = []
    for i in range(1, 41):
        rows.append(("REPAIR", "CLOSED", "2023-01-01T00:00:00.000Z", "2023-01-02T00:00:00.000Z",
                     f"2023-01-{2 + i % 10:02d}T00:00:00.000Z", f"2023-01-{3 + i % 10:02d}T00:00:00.000Z",
                     "0", "1", "0", "ROAD CALL-TARGET" if i % 2 else "NORMAL WEAR"))
    rows.append(("PM", "CLOSED", "2023-01-05T00:00:00.000Z", "2023-01-05T00:00:00.000Z",
                 "2023-01-05T00:00:00.000Z", "2023-01-05T00:00:00.000Z", "0", "1", "0", "PM"))
    con.executemany("insert into ref_cincinnati_closed_2022_2024 values (?,?,?,?,?,?,?,?,?,?)", rows)
    return con


def test_repair_durations_by_job_type_in_days():
    out = derive_repair_durations(_wo_db(), today="2026-10-11", min_rows=10)
    rep = out["by_job_type"]["REPAIR"]
    assert rep["n"] == 40
    assert rep["open_to_closed_days"]["p50"] >= 2
    assert out["by_job_type"]["PM"] is None  # fewer than min_rows
    assert out["resolution_note"].startswith("timestamps are whole days")
    assert out["licence"] == "PUBLIC_DOMAIN (Socrata licenseId, observed)"
    assert rep["lognormal_fit"]["sigma"] >= 0
