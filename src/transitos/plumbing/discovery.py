"""Crosswalk discovery from product reads (Phase 10 Step 2.2/2.3): for a fleet whose identifiers disagree across
products (agency-088), the vehicle crosswalk is worked out from what the products serve, never from generator truth.

1. The anchor product (scheduling: the agency's revenue roster, A85) defines the canonical vehicles. Its records
   become crosswalk rows with their VIN, plate and display name as evidence; the canonical id is derived from the
   roster's own id (`veh-` + normalised fleet number).
2. Every other product's roster record is resolved by the SAME rules resolver the event path uses, at the record's
   own time (a removed gateway just before its removal, an installed one at its install time). MATCH adds a row with
   the record's validity window; AMBIGUOUS and NO_MATCH add nothing and are reported (a human maps them in the panel
   when an event from that id arrives and is parked; A89: not shown in the panel before that).
Product field names are mapped here, as an adapter would (each product spells VIN, plate and dates its own way).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from transitos.contracts.crosswalk import CrosswalkEntry

from .crosswalk import SqliteCrosswalk
from .native_id import norm_fleet
from .resolver import Evidence, RulesResolver


@dataclass(frozen=True)
class RosterRecord:
    product: str
    native_id: str
    entity: str
    vin: str | None = None
    plate: str | None = None
    name: str | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None


def _ts(s: Any) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(str(s))   # Python 3.11 reads a trailing Z


def read_scheduling(rows: list[dict]) -> list[RosterRecord]:
    return [RosterRecord("scheduling", r["id"], "vehicle", r.get("vin"), r.get("plate"), r.get("name")) for r in rows]


def read_telematics(rows: list[dict]) -> list[RosterRecord]:
    return [RosterRecord("telematics", r["id"], "vehicle", r.get("vin"), r.get("licensePlate"), r.get("name"),
                         _ts(r.get("installedAt")), _ts(r.get("removedAt"))) for r in rows]


def read_maintenance(rows: list[dict]) -> list[RosterRecord]:
    return [RosterRecord("maintenance", str(r["asset"]), "asset", r.get("vin"), r.get("plate"), r.get("name"))
            for r in rows]


def anchor_canonical(native_id: str) -> str:
    n = norm_fleet(native_id)
    if n is None:
        raise ValueError(f"blank roster id {native_id!r}")
    return f"veh-{n.lower()}"


def discover(xw: SqliteCrosswalk, agency: str, anchor: list[RosterRecord], others: list[RosterRecord],
             now: datetime) -> list[dict]:
    """Write the anchor rows, then resolve every other record; returns one report row per record."""
    report: list[dict] = []
    for r in sorted(anchor, key=lambda r: r.native_id):
        canon = anchor_canonical(r.native_id)
        xw.resolve(CrosswalkEntry(agency=agency, product=r.product, native_id=r.native_id, entity=r.entity,
                                  canonical_id=canon, vin=r.vin, plate=r.plate, display_name=r.name,
                                  valid_from=r.valid_from, valid_to=r.valid_to))
        report.append({"product": r.product, "native_id": r.native_id, "outcome": "ANCHOR", "canonical_id": canon,
                       "confidence": 1.0, "evidence": [], "notes": []})
    resolver = RulesResolver(xw)
    for r in sorted(others, key=lambda r: (r.product, r.native_id)):
        at = r.valid_to - timedelta(microseconds=1) if r.valid_to else (r.valid_from or now)
        res, notes = resolver.resolve_detailed(agency, r.product, Evidence(r.native_id, r.vin, r.plate, r.name), at)
        canon = res.candidates[0].canonical_id if res.outcome == "MATCH" else None
        if canon:
            xw.resolve(CrosswalkEntry(agency=agency, product=r.product, native_id=r.native_id, entity=r.entity,
                                      canonical_id=canon, valid_from=r.valid_from, valid_to=r.valid_to))
        report.append({"product": r.product, "native_id": r.native_id, "outcome": res.outcome, "canonical_id": canon,
                       "confidence": res.confidence,
                       "candidates": [c.model_dump() for c in res.candidates],
                       "evidence": list(res.candidates[0].evidence) if res.candidates else [], "notes": notes})
    return report


def discover_from_products(xw: SqliteCrosswalk, clients: dict[str, Any], agency: str, now: datetime) -> list[dict]:
    """Read the three vehicle rosters over each product's API (as an adapter would) and run `discover`."""
    def get(product: str, path: str) -> list[dict]:
        r = clients[product].get(path, headers={"X-Agency": agency})
        r.raise_for_status()
        return r.json()

    anchor = read_scheduling(get("scheduling", "/vehicles"))
    others = read_telematics(get("telematics", "/vehicles")) + read_maintenance(get("maintenance", "/assets"))
    return discover(xw, agency, anchor, others, now)
