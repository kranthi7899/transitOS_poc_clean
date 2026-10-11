"""P5: pydantic-validated loaders for the committed derived files, fixtures and config.

Fail loudly: a missing file raises FileNotFoundError, a malformed one raises pydantic ValidationError. The mocks
and the world engine read data ONLY through these functions (never data/raw or data/reference.db).
Needs only pydantic and pyyaml (default install).
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

DEFAULT_ROOT = Path(__file__).resolve().parents[3]
QKEYS = ("p10", "p25", "p50", "p75", "p90")


def _read(root: Path | None, rel: str) -> dict:
    path = Path(root or DEFAULT_ROOT) / rel
    if not path.exists():
        raise FileNotFoundError(f"required data file missing: {path} (run `make data` or restore from git)")
    doc = yaml.safe_load(path.read_text())
    if not isinstance(doc, dict):
        raise TypeError(f"{path} is not a YAML mapping")
    return doc


class _M(BaseModel):
    model_config = ConfigDict(extra="allow")


# ------------------------------------------------------------------------------------------- fault rates
class Quantiles(_M):
    p10: float
    p25: float
    p50: float
    p75: float
    p90: float

    @model_validator(mode="after")
    def _monotonic(self):
        vals = [getattr(self, k) for k in QKEYS]
        if any(v <= 0 for v in vals) or vals != sorted(vals):
            raise ValueError(f"quantiles must be positive and non-decreasing, got {vals}")
        return self


class Dist(_M):
    n: int | None
    miles_between_major_failures: Quantiles
    source: str | None = None


class Cut(_M):
    y2024: Dist | None = None
    pooled_2022_2024: Dist | None = None
    note: str | None = None


class ModeRates(_M):
    name: str
    source: str | None = None
    all_agencies: Cut
    small_fleet_voms_10_50: Cut


class FaultRates(_M):
    source: Literal["NTD breakdowns", "assumption"]
    modes: dict[str, ModeRates] = Field(min_length=1)

    @model_validator(mode="after")
    def _assumption_tagged(self):
        if self.source == "assumption":
            for k, m in self.modes.items():
                if m.source != "assumption":
                    raise ValueError(f"mode {k}: file is assumed, so every mode must carry source: assumption")
        for k, m in self.modes.items():
            if m.all_agencies.pooled_2022_2024 is None:
                raise ValueError(f"mode {k}: pooled_2022_2024 (all agencies) is required")
        return self

    def miles_between(self, mode: str, quantile: str = "p50", cut: str = "all", window: str = "pooled_2022_2024") -> float:
        m = self.modes[mode]
        c = m.all_agencies if cut == "all" else m.small_fleet_voms_10_50
        d = getattr(c, window)
        if d is None:
            raise KeyError(f"no {window} distribution for {mode}/{cut}: {c.note}")
        return getattr(d.miles_between_major_failures, quantile)

    def rate_per_mile(self, mode: str, quantile: str = "p50", **kw) -> float:
        """Poisson rate per revenue mile = 1 / chosen quantile of miles between major failures."""
        return 1.0 / self.miles_between(mode, quantile, **kw)


def load_fault_rates(root: Path | None = None) -> FaultRates:
    return FaultRates.model_validate(_read(root, "data/derived/fault_rates.yaml"))


# --------------------------------------------------------------------------------------------- fleet mix
class TypeMix(_M):
    vehicles: int
    make_share: dict[str, float]
    model_share: dict[str, float]
    seating_capacity_share: dict[str, float]
    length_share: dict[str, float]
    fuel_share: dict[str, float]
    ada_share: float | None
    manufacture_year_share: dict[str, float]

    @model_validator(mode="after")
    def _shares(self):
        for name in ("make_share", "model_share", "seating_capacity_share", "length_share", "fuel_share"):
            d = getattr(self, name)
            if not d or any(not 0 <= v <= 1 for v in d.values()) or abs(sum(d.values()) - 1) > 0.01:
                raise ValueError(f"{name} must hold shares in [0,1] summing to about 1")
        return self


class FleetMix(_M):
    vehicle_type_share: dict[str, float]
    by_type: dict[str, TypeMix]
    model_year_gap: dict

    @model_validator(mode="after")
    def _check(self):
        if abs(sum(self.vehicle_type_share.values()) - 1) > 0.01:
            raise ValueError("vehicle_type_share must sum to about 1")
        for t in ("Bus", "Cutaway"):
            if t not in self.by_type:
                raise ValueError(f"by_type must include {t}")
        if self.model_year_gap.get("source") != "assumption":
            raise ValueError("model_year_gap must be tagged source: assumption (the data has no model year)")
        return self


def load_fleet_mix(root: Path | None = None) -> FleetMix:
    return FleetMix.model_validate(_read(root, "data/derived/fleet_mix.yaml"))


# ---------------------------------------------------------------------------------------- repair durations
class JobDurations(_M):
    n: int
    open_to_closed_days: Quantiles | dict
    lognormal_fit: dict


class RepairDurations(_M):
    licence: str
    by_job_type: dict[str, JobDurations | None]


def load_repair_durations(root: Path | None = None) -> RepairDurations | None:
    """Optional derived file: returns None when absent (the config/shop_behaviour.yaml fallback applies)."""
    path = Path(root or DEFAULT_ROOT) / "data/derived/repair_durations.yaml"
    if not path.exists():
        return None
    return RepairDurations.model_validate(_read(root, "data/derived/repair_durations.yaml"))


# ------------------------------------------------------------------------------------------ J1939 fixture
Severity = Literal["monitor", "service_soon", "pull_vehicle"]


class SpnRow(_M):
    spn: int
    name: str
    system: str
    unit: str
    typical_fmi: list[int]
    name_confidence: Literal["listed_in_research", "unverified"]


class Override(_M):
    spn: int
    fmi: int
    severity: Severity
    why: str


class SeverityRule(_M):
    default: Severity
    fmi_default: dict[int, Severity]
    overrides: list[Override] = []


class J1939(_M):
    version: int
    fmi: dict[int, str]
    severity_rule: SeverityRule
    spn: list[SpnRow] = Field(min_length=10)

    @model_validator(mode="after")
    def _consistent(self):
        spns = [r.spn for r in self.spn]
        if len(spns) != len(set(spns)):
            raise ValueError("duplicate SPN rows")
        for r in self.spn:
            bad = [f for f in r.typical_fmi if f not in self.fmi]
            if bad:
                raise ValueError(f"SPN {r.spn}: FMI {bad} missing from the fmi table")
        return self

    def severity(self, spn: int, fmi: int) -> str:
        """Configurable rule, never ground truth: first matching override, else per-FMI default, else the default."""
        for o in self.severity_rule.overrides:
            if o.spn == spn and o.fmi == fmi:
                return o.severity
        return self.severity_rule.fmi_default.get(fmi, self.severity_rule.default)


def load_j1939(root: Path | None = None) -> J1939:
    return J1939.model_validate(_read(root, "fixtures/j1939_spn_fmi.yaml"))


# ------------------------------------------------------------------------------------------------ configs
class SystemWeight(_M):
    weight: float = Field(ge=0, le=1)
    fta_major_example: bool
    primary_spns: list[int]


class FaultMix(_M):
    source: Literal["assumption"]
    assumption_ids: list[str]
    systems: dict[str, SystemWeight] = Field(min_length=6)

    @model_validator(mode="after")
    def _sum(self):
        total = sum(s.weight for s in self.systems.values())
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"system weights must sum to 1.0, got {total}")
        return self


def load_fault_mix(root: Path | None = None) -> FaultMix:
    return FaultMix.model_validate(_read(root, "config/fault_mix.yaml"))


class Lognormal(_M):
    median: float = Field(gt=0)
    sigma: float = Field(gt=0)


class Prob(_M):
    probability: float = Field(ge=0, le=1)
    hours: Lognormal


class ShopBehaviour(_M):
    source: Literal["assumption"]
    assumption_ids: list[str]
    statuses: dict[str, list[str]]
    pending_to_work_order_hours: Lognormal
    parts_wait: Prob
    deferral_to_pending: Prob
    human_opens_work_order_first_probability: float = Field(ge=0, le=1)
    repair_hours_by_severity: dict[str, Lognormal]

    @model_validator(mode="after")
    def _levels(self):
        if set(self.repair_hours_by_severity) != {"monitor", "service_soon", "pull_vehicle"}:
            raise ValueError("repair_hours_by_severity needs exactly monitor, service_soon, pull_vehicle")
        return self


def load_shop_behaviour(root: Path | None = None) -> ShopBehaviour:
    return ShopBehaviour.model_validate(_read(root, "config/shop_behaviour.yaml"))
