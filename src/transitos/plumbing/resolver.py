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
from decimal import Decimal
from typing import Protocol

from rapidfuzz import fuzz

from transitos.contracts.crosswalk import Candidate, CrosswalkEntry, Resolution
from transitos.data.identifiers import is_valid_vin

from .native_id import norm_fleet, norm_plate, norm_serial, norm_vin

# ASSUMED thresholds (A79, A80): the plan's numbers, tested at their boundaries in tests/plumbing/test_resolver.py
MATCH_MIN = 0.9
LEAD_MIN = 0.1
SCORE_VIN = 1.0
SCORE_PLATE = 0.95
SCORE_FLEET_EXACT = 0.9
SCORE_FLEET_MIN = 0.6
FUZZ_FLOOR = 80.0          # rapidfuzz ratio (0-100) below which a fleet number is no candidate
VEHICLE_ENTITIES = ("vehicle", "asset")
_MATCH_MIN = Decimal("0.9")  # thresholds compared in exact decimal (F10): 0.95 - 0.85 is 0.1, and
_LEAD_MIN = Decimal("0.1")   # 0.89999999999 is NOT rounded up to 0.9


def _dec(x: float) -> Decimal:
    return Decimal(repr(x))


def native_norm(product: str, s: str | None) -> str | None:
    """Scheduling roster ids are fleet numbers; every other product's native id is a serial/asset id (F3, A141)."""
    return norm_fleet(s) if product == "scheduling" else norm_serial(s)


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
    if _dec(top) >= _MATCH_MIN and _dec(top) - _dec(runner) >= _LEAD_MIN:
        outcome = "MATCH"
    else:
        # F5 (A140): any candidate without an automatic MATCH goes to a human with ALL candidates; NO_MATCH only
        # when there is no candidate at all
        outcome = "AMBIGUOUS"
    return Resolution(outcome=outcome, confidence=top, candidates=cands)  # type: ignore[arg-type]


def fleet_score(ratio: float) -> float | None:
    if ratio >= 100.0:
        return SCORE_FLEET_EXACT
    if ratio < FUZZ_FLOOR:
        return None
    return SCORE_FLEET_MIN + (SCORE_FLEET_EXACT - SCORE_FLEET_MIN) * (ratio - FUZZ_FLOOR) / (100.0 - FUZZ_FLOOR)


def _hard_evidence(evidence: Evidence) -> tuple[str | None, str | None, str | None]:
    """(normalised plate, normalised VIN or None when dirty/absent, note)."""
    vin = norm_vin(evidence.vin)
    note = None
    if vin is not None and not is_valid_vin(vin):
        note = f"event VIN {evidence.vin!r} fails its check digit or length: dirty VIN, ignored"
        vin = None
    return norm_plate(evidence.plate), vin, note


def _hard_hits(r: CrosswalkEntry, ev_vin: str | None, ev_plate: str | None) -> list[tuple[float, str]]:
    out = []
    if ev_vin is not None and (rv := norm_vin(r.vin)) is not None and is_valid_vin(rv) and rv == ev_vin:
        out.append((SCORE_VIN, "vin exact"))
    if ev_plate is not None and norm_plate(r.plate) == ev_plate:
        out.append((SCORE_PLATE, "plate exact"))
    return out


class RulesResolver:
    def __init__(self, source: RowSource):
        self.source = source

    def contradiction(self, agency: str, product: str, evidence: Evidence, at: datetime,
                      canonical: str) -> Resolution | None:
        """F1 (A141): the exact native id maps to `canonical`, but the event's VIN or plate EXACTLY matches a
        DIFFERENT vehicle's valid row and matches none of `canonical`'s rows -> AMBIGUOUS with both. Missing or
        dirty evidence is unknown (no contradiction); a name alone never overrides the id."""
        hits = self._hard_hits_at(agency, evidence, at)
        others = {c: ev for c, ev in hits.items() if c != canonical}
        if not others or canonical in hits:
            return None
        cands = [Candidate(canonical_id=canonical, score=1.0,
                           evidence=[f"exact {product} id {evidence.native_id} (contradicted)"])]
        cands += [Candidate(canonical_id=c, score=SCORE_VIN if any(e.startswith("vin") for e in ev) else SCORE_PLATE,
                            evidence=sorted(ev)) for c, ev in others.items()]
        return Resolution(outcome="AMBIGUOUS", confidence=1.0,
                          candidates=sorted(cands, key=lambda c: (-c.score, c.canonical_id)))

    def _hard_hits_at(self, agency: str, evidence: Evidence, at: datetime) -> dict[str, list[str]]:
        """canonical id -> the exact VIN / plate hits of the event's evidence on that vehicle's rows valid at `at`."""
        ev_plate, ev_vin, _ = _hard_evidence(evidence)
        hits: dict[str, list[str]] = {}
        if ev_plate is None and ev_vin is None:
            return hits
        for r in self.source.rows(agency):
            if r.agency == agency and r.entity in VEHICLE_ENTITIES and valid_at(r, at):
                for _, why in _hard_hits(r, ev_vin, ev_plate):
                    hits.setdefault(r.canonical_id, []).append(f"{why} ({r.product} {r.native_id})")
        return hits

    def conflict_notes(self, agency: str, evidence: Evidence, at: datetime, canonical: str) -> list[str]:
        """G2 (A149): on the fast path, when the event's VIN or plate matches `canonical`'s own rows (so it is no F1
        contradiction and the event resolves to `canonical`) but ALSO exactly matches another vehicle's row valid at
        `at`, each such hit is returned as a note `<vin|plate>_matches_other_vehicle:<canonical>` for the audit.
        Never changes the outcome. Missing or dirty evidence, and ended rows, are no conflict."""
        hits = self._hard_hits_at(agency, evidence, at)
        if canonical not in hits:
            return []
        return sorted({f"{why.split(' ', 1)[0]}_matches_other_vehicle:{c}"
                       for c, whys in hits.items() if c != canonical for why in whys})

    def resolve(self, agency: str, product: str, evidence: Evidence, at: datetime) -> Resolution:
        return self.resolve_detailed(agency, product, evidence, at)[0]

    def resolve_detailed(self, agency: str, product: str, evidence: Evidence,
                         at: datetime) -> tuple[Resolution, list[str]]:
        notes: list[str] = []
        rows = [r for r in self.source.rows(agency) if r.agency == agency and r.entity in VEHICLE_ENTITIES]
        valid = [r for r in rows if valid_at(r, at)]
        ev_native = native_norm(product, evidence.native_id)
        ev_name = norm_fleet(evidence.name)
        ev_plate, ev_vin, vin_note = _hard_evidence(evidence)
        if vin_note:
            notes.append(vin_note)
        best: dict[str, tuple[float, set[str]]] = {}

        def hit(canon: str, score: float, why: str) -> None:
            s, ev = best.get(canon, (0.0, set()))
            ev.add(why)
            best[canon] = (max(s, score), ev)

        for r in valid:
            where = f"{r.product} {r.native_id}"
            for score, why in _hard_hits(r, ev_vin, ev_plate):
                hit(r.canonical_id, score, f"{why} ({where})")
            row_tokens = [t for t in (norm_fleet(r.display_name),
                                      native_norm(product, r.native_id) if r.product == product else None) if t]
            for et in (t for t in (ev_native, ev_name) if t):
                for rt in row_tokens:
                    score = fleet_score(fuzz.ratio(et, rt))
                    if score is not None:
                        hit(r.canonical_id, score, f"fleet number {et} ~ {rt} ({where})")
        res = decide([Candidate(canonical_id=c, score=s, evidence=sorted(ev)) for c, (s, ev) in best.items()])
        known_elsewhen = ev_native is not None and any(
            r.product == product and native_norm(product, r.native_id) == ev_native for r in rows) and not any(
            r.product == product and native_norm(product, r.native_id) == ev_native for r in valid)
        if known_elsewhen:
            notes.append(f"{product} id {evidence.native_id!r} is known but has no crosswalk row valid at "
                         f"{utc(at).isoformat()}: never auto-matched")
            if res.outcome == "MATCH":
                res = res.model_copy(update={"outcome": "AMBIGUOUS"})
        return res, notes
