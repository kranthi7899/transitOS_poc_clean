"""Native -> canonical mapping for seeding the crosswalk (veh-<n>, op-<n>)."""
from __future__ import annotations

from datetime import datetime

from transitos.contracts.crosswalk import CrosswalkEntry

from .clock import DEFAULT_START
from .seed import AGENCIES, build_fleet


def crosswalk_entries(agency: str, now: datetime = DEFAULT_START) -> list[CrosswalkEntry]:
    fleet = build_fleet(agency, now)
    out: list[CrosswalkEntry] = []
    for v in fleet.vehicles:
        out += [
            CrosswalkEntry(agency=agency, product="scheduling", native_id=v["sched"], entity="vehicle", canonical_id=v["canonical"]),
            CrosswalkEntry(agency=agency, product="telematics", native_id=v["tele"], entity="vehicle", canonical_id=v["canonical"]),
            CrosswalkEntry(agency=agency, product="maintenance", native_id=v["maint"], entity="asset", canonical_id=v["canonical"]),
        ]
    for o in fleet.operators:
        out.append(CrosswalkEntry(agency=agency, product="workforce", native_id=o["native"], entity="operator", canonical_id=o["canonical"]))
    return out


def all_crosswalk_entries() -> list[CrosswalkEntry]:
    return [e for a in AGENCIES for e in crosswalk_entries(a)]
