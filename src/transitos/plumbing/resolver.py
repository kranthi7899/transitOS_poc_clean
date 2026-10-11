"""Identity resolver (Phase 10 Step 2.3, decision D-E): plain-Python rules behind a `Resolver` protocol, so a
probabilistic linker (for example Splink) could replace it later.

Order, per the phase plan:
  1. normalise the event's evidence (transitos.plumbing.native_id);
  2. keep only crosswalk rows of the event's agency (the namespace) and of a vehicle/asset entity that are VALID at
     the event's own timestamp (valid_from inclusive, valid_to exclusive; naive times are UTC, A81);
  3. score each canonical id: exact VIN with a valid check digit 1.0 (a 17-character VIN whose check digit fails is a
     dirty-VIN signal and is ignored, never a match); exact normalised plate 0.95; fleet number with rapidfuzz
     0.6-0.9 (0.9 only for an exact normalised fleet number; ratio below 80 is no candidate, A80). A candidate's
     score is the best of its rules; its evidence lists every rule that fired;
  4. decide (`decide`): MATCH if top >= 0.9 and lead over the runner-up >= 0.1; AMBIGUOUS if two candidates are
     within 0.1, or if a LONE candidate is below 0.9 (a human decides); otherwise NO_MATCH (A79).
Missing evidence (blank VIN, plate or name) is unknown: it never adds a candidate and never removes one.
A native id that this product used before but that has no row valid at the event time (a swapped-out device, a
retired number) is never auto-matched: a MATCH is downgraded to AMBIGUOUS (A82).
Deterministic: candidates sorted by score descending, then canonical id.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from rapidfuzz import fuzz

from transitos.contracts.crosswalk import Candidate, CrosswalkEntry, Resolution
from transitos.data.identifiers import is_valid_vin

from .native_id import norm_fleet, norm_plate, norm_vin

# ASSUMED thresholds (A79, A80): the plan's numbers, tested at their boundaries in tests/plumbing/test_resolver.py
MATCH_MIN = 0.9
LEAD_MIN = 0.1
SCORE_VIN = 1.0
SCORE_PLATE = 0.95
SCORE_FLEET_EXACT = 0.9
SCORE_FLEET_MIN = 0.6
FUZZ_FLOOR = 80.0          # rapidfuzz ratio (0-100) below which a fleet number is no candidate
VEHICLE_ENTITIES = ("vehicle", "asset")
_EPS = 9                   # decimals for threshold comparisons (0.95 - 0.85 is 0.1, not 0.0999...)


@dataclass(frozen=True)
class Evidence:
    """What a real adapter sees about the vehicle in one event: its native id in the publishing product, and the
    VIN, plate and display name the product reported (each optional)."""
    native_id: str
    vin: str | None = None
    plate: str | None = None
    name: str | None = None


class RowSource(Protocol):
    def rows(self, agency: str) -> list[CrosswalkEntry]: ...


class StaticRows:
    def __init__(self, rows: Iterable[CrosswalkEntry]):
        self._rows = list(rows)

    def rows(self, agency: str) -> list[CrosswalkEntry]:
        return [r for r in self._rows if r.agency == agency]


class Resolver(Protocol):
    def resolve(self, agency: str, product: str, evidence: Evidence, at: datetime) -> Resolution: ...


def utc(t: datetime) -> datetime:
    return t.replace(tzinfo=UTC) if t.tzinfo is None else t.astimezone(UTC)


def valid_at(row: CrosswalkEntry, at: datetime) -> bool:
    """valid_from inclusive, valid_to exclusive; an open bound is unbounded."""
    t = utc(at)
    if row.valid_from is not None and t < utc(row.valid_from):
        return False
    return not (row.valid_to is not None and t >= utc(row.valid_to))


def decide(candidates: list[Candidate]) -> Resolution:
    cands = sorted(candidates, key=lambda c: (-c.score, c.canonical_id))
    if not cands:
        return Resolution(outcome="NO_MATCH", confidence=0.0, candidates=[])
    top = cands[0].score
    runner = cands[1].score if len(cands) > 1 else 0.0
    lead = round(top - runner, _EPS)
    if round(top, _EPS) >= MATCH_MIN and lead >= LEAD_MIN:
        outcome = "MATCH"
    elif len(cands) > 1 and lead < LEAD_MIN:
        outcome = "AMBIGUOUS"
    elif len(cands) == 1:
        outcome = "AMBIGUOUS"          # a lone candidate below the threshold goes to a human
    else:
        outcome = "NO_MATCH"
    return Resolution(outcome=outcome, confidence=top, candidates=cands)  # type: ignore[arg-type]


def fleet_score(ratio: float) -> float | None:
    if ratio >= 100.0:
        return SCORE_FLEET_EXACT
    if ratio < FUZZ_FLOOR:
        return None
    return SCORE_FLEET_MIN + (SCORE_FLEET_EXACT - SCORE_FLEET_MIN) * (ratio - FUZZ_FLOOR) / (100.0 - FUZZ_FLOOR)


class RulesResolver:
    def __init__(self, source: RowSource):
        self.source = source

    def resolve(self, agency: str, product: str, evidence: Evidence, at: datetime) -> Resolution:
        return self.resolve_detailed(agency, product, evidence, at)[0]

    def resolve_detailed(self, agency: str, product: str, evidence: Evidence,
                         at: datetime) -> tuple[Resolution, list[str]]:
        notes: list[str] = []
        rows = [r for r in self.source.rows(agency) if r.agency == agency and r.entity in VEHICLE_ENTITIES]
        valid = [r for r in rows if valid_at(r, at)]
        ev_native = norm_fleet(evidence.native_id)
        ev_name = norm_fleet(evidence.name)
        ev_plate = norm_plate(evidence.plate)
        ev_vin = norm_vin(evidence.vin)
        if ev_vin is not None and not is_valid_vin(ev_vin):
            notes.append(f"event VIN {evidence.vin!r} fails its check digit or length: dirty VIN, ignored")
            ev_vin = None
        best: dict[str, tuple[float, set[str]]] = {}

        def hit(canon: str, score: float, why: str) -> None:
            s, ev = best.get(canon, (0.0, set()))
            ev.add(why)
            best[canon] = (max(s, score), ev)

        for r in valid:
            where = f"{r.product} {r.native_id}"
            if ev_vin is not None and (rv := norm_vin(r.vin)) is not None and is_valid_vin(rv) and rv == ev_vin:
                hit(r.canonical_id, SCORE_VIN, f"vin exact ({where})")
            if ev_plate is not None and norm_plate(r.plate) == ev_plate:
                hit(r.canonical_id, SCORE_PLATE, f"plate exact ({where})")
            row_tokens = [t for t in (norm_fleet(r.display_name),
                                      norm_fleet(r.native_id) if r.product == product else None) if t]
            for et in (t for t in (ev_native, ev_name) if t):
                for rt in row_tokens:
                    score = fleet_score(fuzz.ratio(et, rt))
                    if score is not None:
                        hit(r.canonical_id, score, f"fleet number {et} ~ {rt} ({where})")
        res = decide([Candidate(canonical_id=c, score=round(s, 6), evidence=sorted(ev)) for c, (s, ev) in best.items()])
        known_elsewhen = ev_native is not None and any(
            r.product == product and norm_fleet(r.native_id) == ev_native for r in rows) and not any(
            r.product == product and norm_fleet(r.native_id) == ev_native for r in valid)
        if known_elsewhen:
            notes.append(f"{product} id {evidence.native_id!r} is known but has no crosswalk row valid at "
                         f"{utc(at).isoformat()}: never auto-matched")
            if res.outcome == "MATCH":
                res = res.model_copy(update={"outcome": "AMBIGUOUS"})
        return res, notes
