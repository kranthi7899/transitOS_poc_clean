"""Native -> canonical mapping for seeding the crosswalk (veh-<n>, op-<n>).

Phase 10 Step 2: rows carry validity (a swapped telematics gateway `tele_prev`, a renumbered maintenance asset
`maint_prev`) and, where the fleet declares them, the scheduling record's VIN, plate and name as evidence. A product
the vehicle is absent from gets no row. Generated profiles (`seed.GENERATED`, agency-088) get NO vehicle rows here:
their vehicle crosswalk is worked out from what the products serve (`transitos.plumbing.discovery`), never from the
generator's truth.
"""
from __future__ import annotations

from datetime import datetime

from transitos.contracts.crosswalk import CrosswalkEntry

from .clock import DEFAULT_START, parse_ts
from .seed import AGENCIES, GENERATED, build_fleet


def _ts(s: str | None) -> datetime | None:
    return parse_ts(s) if s else None


def vehicle_entries(agency: str, v: dict) -> list[CrosswalkEntry]:
    def row(product: str, native: str, entity: str, **kw) -> CrosswalkEntry:
        return CrosswalkEntry(agency=agency, product=product, native_id=native, entity=entity,
                              canonical_id=v["canonical"], **kw)

    out: list[CrosswalkEntry] = []
    if v.get("sched") is not None:
        out.append(row("scheduling", v["sched"], "vehicle", vin=v.get("sched_vin"), plate=v.get("sched_plate"),
                       display_name=v.get("sched_name")))
    if v.get("tele") is not None:
        prev = v.get("tele_prev")
        if prev:
            out.append(row("telematics", prev["id"], "vehicle", valid_to=_ts(prev["removed_at"])))
        out.append(row("telematics", v["tele"], "vehicle",
                       valid_from=_ts(v.get("tele_installed_at") or (prev or {}).get("removed_at"))))
    if v.get("maint") is not None:
        prev = v.get("maint_prev")
        if prev:
            out.append(row("maintenance", prev["id"], "asset", valid_to=_ts(prev["renumbered_at"])))
        out.append(row("maintenance", v["maint"], "asset", valid_from=_ts((prev or {}).get("renumbered_at"))))
    return out


def crosswalk_entries(agency: str, now: datetime = DEFAULT_START) -> list[CrosswalkEntry]:
    fleet = build_fleet(agency, now)
    out: list[CrosswalkEntry] = []
    if agency not in GENERATED:
        for v in fleet.vehicles:
            out += vehicle_entries(agency, v)
    for o in fleet.operators:
        out.append(CrosswalkEntry(agency=agency, product="workforce", native_id=o["native"], entity="operator", canonical_id=o["canonical"]))
    return out


def all_crosswalk_entries() -> list[CrosswalkEntry]:
    return [e for a in AGENCIES for e in crosswalk_entries(a)]
