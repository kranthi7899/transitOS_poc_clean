"""Facts the clearance rules read: plain objects plus, for every field, the basis it came with.

Resolution order for a rule's data (amendment A, Q3): native, derived from other data, agency default tagged
ASSUMED, unknown. A `CapabilityProfile` states which fields a product supplies natively; a field it does not
list is unknown. Nothing here does I/O except `load_tables` (two small YAML files, cached)."""
from __future__ import annotations

import functools
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from transitos.contracts.clearance import Basis
from transitos.contracts.objects import Block, Needs, Operator, Vehicle

BASIS_ORDER: dict[str, int] = {"native": 0, "derived": 1, "assumed": 2, "unknown": 3}

# Fields each product can supply (the vocabulary of capability profiles).
SCHEDULING_FIELDS = ("seats", "wheelchair_positions", "lift_ok", "type", "length_ft", "blocks")
WORKFORCE_FIELDS = ("qualifications", "licence_class", "endorsements", "trained_vehicle_types",
                    "training_valid_to", "lift_trained", "hours_today", "hours_week", "availability",
                    "duty_times", "medical_valid_to")
PRODUCT_FIELDS = {"scheduling": SCHEDULING_FIELDS, "workforce": WORKFORCE_FIELDS}
# A capability-profile field -> the contract field name(s) on the record that carry it (C-2 `unknown_fields`
# lists contract field names). Fields not listed here carry the same name on the record.
RECORD_FIELDS: dict[str, tuple[str, ...]] = {
    "availability": ("available_from", "available_to"),
    "duty_times": ("last_duty_end", "duty_start_today"),
}
CONFIG_DIR = Path(__file__).resolve().parent.parent / "tools" / "config"


def weakest(*bases: Basis) -> Basis:
    """The least certain of several bases (native < derived < assumed < unknown)."""
    return max(bases, key=lambda b: BASIS_ORDER[b])


@dataclass(frozen=True)
class Datum:
    """A value together with how we came to know it."""
    value: Any
    basis: Basis
    note: str = ""


@dataclass(frozen=True)
class CapabilityProfile:
    """Which fields one product supplies, and on what basis. A field not listed is unknown."""
    product: str
    fields: Mapping[str, Basis]

    def basis(self, name: str) -> Basis:
        return self.fields.get(name, "unknown")

    def to_dict(self) -> dict[str, Any]:
        return {"product": self.product, "fields": dict(self.fields)}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> CapabilityProfile:
        return cls(d["product"], dict(d["fields"]))


def native_profile(product: str, drop: Iterable[str] = ()) -> CapabilityProfile:
    """A profile in which the product supplies every known field natively, except those in `drop`
    (which become unknown)."""
    dropped = set(drop)
    return CapabilityProfile(product, {f: ("unknown" if f in dropped else "native")
                                       for f in PRODUCT_FIELDS[product]})


@dataclass(frozen=True)
class Tables:
    licence_order: tuple[str, ...]
    passenger_endorsement_code: str
    vehicle_requirements: Mapping[str, Mapping[str, Any]]
    vehicle_order: tuple[str, ...]
    typical_length_ft: Mapping[str, float]


@functools.lru_cache(maxsize=4)
def load_tables(config_dir: str | None = None) -> Tables:
    base = Path(config_dir) if config_dir else CONFIG_DIR
    lic = yaml.safe_load((base / "licence_classes.yaml").read_text())
    veh = yaml.safe_load((base / "vehicle_classes.yaml").read_text())
    return Tables(tuple(lic["licence_order"]), lic.get("passenger_endorsement_code", "P"),
                  dict(lic["vehicle_types"]), tuple(veh["order"]), dict(veh.get("typical_length_ft", {})))


Interval = tuple[datetime, datetime, str]


def commitments_from_blocks(blocks: Iterable[Block]) -> dict[str, list[Interval]]:
    """Other scheduled work per resource, from a list of scheduled blocks (vehicle and operator)."""
    out: dict[str, list[Interval]] = {}
    for b in blocks:
        out.setdefault(f"vehicle:{b.vehicle_id}", []).append((b.start, b.end, b.id))
        if b.operator_id:
            out.setdefault(f"operator:{b.operator_id}", []).append((b.start, b.end, b.id))
    return out


@dataclass
class Facts:
    """Everything an option is judged against.

    blocks       the affected blocks being re-planned, by id
    vehicles     candidate spare vehicles by id
    operators    candidate operators and the blocks' current operators by id
    ready_count  ready spares (trigger vehicle excluded) free over the horizon; None = unknown
    commitments  scheduled work per resource ("vehicle:<id>" / "operator:<id>"), the whole feed (it may include
                 `blocks` themselves: the commitments an option replaces are ignored there, F12/A150);
                 None = the schedule feed is not available (double-booking then fails closed)
    profiles     capability profile per product ("scheduling", "workforce")
    """
    blocks: dict[str, Block]
    vehicles: dict[str, Vehicle]
    operators: dict[str, Operator]
    ready_count: int | None
    commitments: dict[str, list[Interval]] | None
    profiles: dict[str, CapabilityProfile] = field(default_factory=dict)
    trigger_vehicle_id: str = ""
    tables: Tables = field(default_factory=load_tables)
    # S2 fix round F4 (A142): maintenance asset status per vehicle id, as the maintenance product reports it
    # ("active", "retired", ...), "missing" (no maintenance asset), "unreachable"; None = no asset-status feed
    asset_status: dict[str, str] | None = None
    # R4-1 (A143): how many of the spares counted in ready_count have a maintenance status that is not known
    # active (rules.count_ready); > 0 makes last_spare unverifiable. REQUIRED, keyword-only (R5-2, A163): a caller
    # that forgot it would otherwise claim every counted spare known active (fail open)
    ready_unknown: int = field(kw_only=True)

    def basis(self, product: str, name: str) -> Basis:
        p = self.profiles.get(product)
        return p.basis(name) if p else "unknown"

    def record_basis(self, product: str, name: str, record: Any, fields: Iterable[str] | None = None) -> Basis:
        """Basis of one field for ONE record (C-2, F3): a contract field the source left blank or absent for this
        record (listed in `record.unknown_fields`) is unknown, ahead of the product-level capability profile.
        `fields` narrows a multi-field capability (e.g. only `last_duty_end` of `duty_times`)."""
        listed = set(getattr(record, "unknown_fields", None) or ())
        if listed & set(fields if fields is not None else RECORD_FIELDS.get(name, (name,))):
            return "unknown"
        return self.basis(product, name)

    # ---- JSON round trip (the planner carries facts inside the model-facing dict and the checkpoint) ----
    def to_dict(self) -> dict[str, Any]:
        return {
            "blocks": {k: b.model_dump(mode="json") for k, b in self.blocks.items()},
            "vehicles": {k: v.model_dump(mode="json") for k, v in self.vehicles.items()},
            "operators": {k: o.model_dump(mode="json") for k, o in self.operators.items()},
            "ready_count": self.ready_count,
            "commitments": None if self.commitments is None else {
                k: [[s.isoformat(), e.isoformat(), bid] for s, e, bid in v] for k, v in self.commitments.items()},
            "profiles": {k: p.to_dict() for k, p in self.profiles.items()},
            "trigger_vehicle_id": self.trigger_vehicle_id,
            "asset_status": None if self.asset_status is None else dict(self.asset_status),
            "ready_unknown": self.ready_unknown,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Facts:
        return cls(
            blocks={k: Block.model_validate(b) for k, b in d["blocks"].items()},
            vehicles={k: Vehicle.model_validate(v) for k, v in d["vehicles"].items()},
            operators={k: Operator.model_validate(o) for k, o in d["operators"].items()},
            ready_count=d["ready_count"],
            commitments=None if d["commitments"] is None else {
                k: [(datetime.fromisoformat(s), datetime.fromisoformat(e), bid) for s, e, bid in v]
                for k, v in d["commitments"].items()},
            profiles={k: CapabilityProfile.from_dict(p) for k, p in d["profiles"].items()},
            trigger_vehicle_id=d.get("trigger_vehicle_id", ""),
            asset_status=None if d.get("asset_status") is None else dict(d["asset_status"]),
            ready_unknown=int(d["ready_unknown"]))   # R5-2 (A163): never defaulted


__all__ = ["BASIS_ORDER", "PRODUCT_FIELDS", "RECORD_FIELDS", "SCHEDULING_FIELDS", "WORKFORCE_FIELDS", "Basis", "CapabilityProfile",
           "Datum", "Facts", "Needs", "Tables", "commitments_from_blocks", "load_tables", "native_profile",
           "weakest"]
