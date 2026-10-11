"""Loaders: validate committed files, fail loudly on missing or malformed ones. Reads only committed files."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from transitos.data import loaders
from transitos.data.check import check_all
from transitos.data.check import main as check_main

REPO = Path(__file__).resolve().parents[2]


def test_committed_files_all_validate():
    report = check_all(REPO)
    assert report.ok, report.errors
    names = {n for n, _ in report.checked}
    assert {"fault_rates", "fleet_mix", "j1939_spn_fmi", "fault_mix", "shop_behaviour"} <= names


def test_fault_rate_helper_returns_miles_and_rate():
    fr = loaders.load_fault_rates(REPO)
    miles = fr.miles_between("MB", "p50")
    assert miles > 1000
    assert fr.rate_per_mile("MB", "p50") == pytest.approx(1 / miles)
    assert fr.miles_between("MB", "p50", cut="small_fleet", window="pooled_2022_2024") > 0


def test_fault_mix_weights_sum_to_one_and_spns_exist():
    mix = loaders.load_fault_mix(REPO)
    assert sum(s.weight for s in mix.systems.values()) == pytest.approx(1.0)
    spns = {r.spn for r in loaders.load_j1939(REPO).spn}
    for s in mix.systems.values():
        assert set(s.primary_spns) <= spns


def test_j1939_severity_rule_is_a_rule():
    j = loaders.load_j1939(REPO)
    assert len(j.spn) >= 12
    assert j.severity(110, 0) == "pull_vehicle"
    assert j.severity(168, 31) == "monitor"
    assert j.severity(9999, 2) == "monitor"


def _copy_repo_files(tmp_path: Path) -> Path:
    for rel in ["data/derived/fault_rates.yaml", "data/derived/fleet_mix.yaml", "fixtures/j1939_spn_fmi.yaml",
                "config/fault_mix.yaml", "config/shop_behaviour.yaml"]:
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / rel, dst)
    return tmp_path


def test_missing_file_fails_loudly(tmp_path):
    root = _copy_repo_files(tmp_path)
    (root / "config/fault_mix.yaml").unlink()
    with pytest.raises(FileNotFoundError, match="fault_mix.yaml"):
        loaders.load_fault_mix(root)
    report = check_all(root)
    assert not report.ok and any("fault_mix" in e for e in report.errors)
    assert check_main([str(root)]) == 1


def test_malformed_file_fails_loudly(tmp_path):
    root = _copy_repo_files(tmp_path)
    data = yaml.safe_load((root / "config/fault_mix.yaml").read_text())
    data["systems"]["brakes"]["weight"] = 0.9  # weights no longer sum to 1
    (root / "config/fault_mix.yaml").write_text(yaml.safe_dump(data))
    with pytest.raises(ValidationError):
        loaders.load_fault_mix(root)
    assert not check_all(root).ok


def test_quantiles_must_be_monotonic(tmp_path):
    root = _copy_repo_files(tmp_path)
    data = yaml.safe_load((root / "data/derived/fault_rates.yaml").read_text())
    q = data["modes"]["MB"]["all_agencies"]["y2024"]["miles_between_major_failures"]
    q["p10"], q["p90"] = q["p90"], q["p10"]
    (root / "data/derived/fault_rates.yaml").write_text(yaml.safe_dump(data))
    with pytest.raises(ValidationError):
        loaders.load_fault_rates(root)


def test_assumed_source_must_be_tagged_in_every_mode(tmp_path):
    root = _copy_repo_files(tmp_path)
    data = yaml.safe_load((root / "data/derived/fault_rates.yaml").read_text())
    data["source"] = "assumption"  # claims assumption at the top, but modes are not tagged
    (root / "data/derived/fault_rates.yaml").write_text(yaml.safe_dump(data))
    with pytest.raises(ValidationError):
        loaders.load_fault_rates(root)


def test_repair_durations_optional(tmp_path):
    root = _copy_repo_files(tmp_path)
    assert loaders.load_repair_durations(root) is None  # not copied: optional, absent is fine
    assert check_all(root).ok
    (root / "data/derived").mkdir(exist_ok=True)
    (root / "data/derived/repair_durations.yaml").write_text("by_job_type: not-a-mapping\n")
    assert not check_all(root).ok  # present but malformed is an error
