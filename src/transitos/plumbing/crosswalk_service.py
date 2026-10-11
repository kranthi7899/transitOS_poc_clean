"""events.raw -> (crosswalk) -> events.resolved keyed by canonical vehicle ID; unknown IDs park + events.failed.

Exactly-once effect on top of at-least-once: processing, processed-marker and outbox rows share one transaction;
the relay publishes afterwards. Raw replay is a no-op for events already resolved.
"""
from __future__ import annotations

import uuid

from transitos.contracts.crosswalk import Candidate, CrosswalkEntry, Resolution
from transitos.contracts.envelope import (
    TOPIC_FAILED,
    TOPIC_RAW,
    TOPIC_RESOLVED,
    CloudEvent,
)
from transitos.contracts.events import PUBLISHER, VehicleRefs

from . import outbox
from .bus_sqlite import SqliteBus
from .crosswalk import SqliteCrosswalk
from .db import Db
from .kafka_bus import KafkaBus
from .relay import Relay
from .resolver import Evidence, RulesResolver

GROUP = "crosswalk"

SCHEMA = """
CREATE TABLE IF NOT EXISTS xw_processed(
  grp TEXT NOT NULL, event_id TEXT NOT NULL, status TEXT NOT NULL,  -- resolved | parked
  PRIMARY KEY(grp, event_id));
"""

# event type -> (data field holding the native id, entity)
NATIVE_FIELD = {
    "VehicleFaultDetected": ("native_vehicle_id", "vehicle"),
    "DefectReported": ("native_vehicle_id", "vehicle"),
    "VehicleStatusChanged": ("native_vehicle_id", "vehicle"),
    "PMOverdue": ("native_asset_id", "vehicle"),
    "WorkOrderOpened": ("native_asset_id", "vehicle"),
    "WorkOrderClosed": ("native_asset_id", "vehicle"),
    "OperatorAvailabilityChanged": ("native_operator_id", "operator"),
}


class CrosswalkService:
    def __init__(self, db: Db, bus: SqliteBus | KafkaBus, crosswalk: SqliteCrosswalk, group: str = GROUP,
                 resolver: RulesResolver | None = None):
        self.db, self.bus, self.xw, self.group = db, bus, crosswalk, group
        self.resolver = resolver or RulesResolver(crosswalk)
        db.script(SCHEMA)
        outbox.ensure_schema(db)
        self.relay = Relay(db, bus)

    # --- enrichment -------------------------------------------------------------------------
    def _enrich(self, ev: CloudEvent, canonical: str, entity: str) -> CloudEvent:
        data = dict(ev.data)
        if entity == "vehicle":
            ids = self.xw.all_ids(ev.agencyid, canonical)
            data["refs"] = VehicleRefs(
                canonical=canonical, telematics=ids.get("telematics"),
                maintenance=ids.get("maintenance"), scheduling=ids.get("scheduling")).model_dump()
        else:
            data["operator_canonical"] = canonical
        return ev.model_copy(update={"data": data, "canonicalid": canonical})

    def _identify(self, ev: CloudEvent, product: str, native: str,
                  entity: str) -> tuple[str | None, Resolution | None, list[str]]:
        """(canonical or None, resolution, notes). Fast path: exactly one canonical id mapped to this native id
        valid at the event's own time. Otherwise vehicle events go to the resolver; operators stay exact-only."""
        rows = self.xw.candidates(ev.agencyid, product, native, at=ev.time)
        canon = sorted({r.canonical_id for r in rows})
        if len(canon) == 1:
            return canon[0], None, []
        if len(canon) > 1:   # overlapping validity windows for one native id: a data conflict, a human decides
            res = Resolution(outcome="AMBIGUOUS", confidence=1.0, candidates=[
                Candidate(canonical_id=c, score=1.0, evidence=[f"exact {product} id {native} (overlapping rows)"])
                for c in canon])
            return None, res, ["overlapping crosswalk rows for one native id"]
        if entity != "vehicle":
            return None, Resolution(outcome="NO_MATCH", confidence=0.0, candidates=[]), []
        d = ev.data
        res, notes = self.resolver.resolve_detailed(
            ev.agencyid, product, Evidence(native_id=native, vin=d.get("native_vin"), plate=d.get("native_plate"),
                                           name=d.get("native_name")), ev.time)
        return (res.candidates[0].canonical_id if res.outcome == "MATCH" else None), res, notes

    @staticmethod
    def _reason(entity: str, product: str, native: str, res: Resolution | None, notes: list[str]) -> str:
        if res is not None and res.outcome == "AMBIGUOUS":
            cands = ", ".join(f"{c.canonical_id} ({c.score:.3f})" for c in res.candidates)
            return f"ambiguous {entity} {product}:{native}: candidates {cands}"
        errors = [n for n in notes if n.startswith("resolver_error")]
        return f"unresolved {entity} {product}:{native}" + (f" ({errors[0]})" if errors else "")

    def _handle(self, conn, ev: CloudEvent) -> None:
        row = conn.execute("SELECT status FROM xw_processed WHERE grp=? AND event_id=?", (self.group, ev.id)).fetchone()
        if row and row["status"] == "resolved":
            return
        field, entity = NATIVE_FIELD[ev.type]
        product = PUBLISHER[ev.type]
        native = str(ev.data[field])
        try:
            canonical, res, notes = self._identify(ev, product, native, entity)
        except Exception as e:  # noqa: BLE001 - fail closed (A83): park and report, never drop, never guess
            canonical, res = None, Resolution(outcome="NO_MATCH", confidence=0.0, candidates=[])
            notes = [f"resolver_error: {type(e).__name__}: {e}"[:300]]
        if canonical is None:
            # parked (again or for the first time); a republished event that still does not resolve goes back in
            # the queue rather than being dropped, and is reported on events.failed only once
            self.xw.add_unresolved_in(conn, ev.agencyid, product, native, ev, res, notes)
            if row:
                return
            failed = ev.model_copy(update={"failurereason": self._reason(entity, product, native, res, notes)})
            outbox.add(conn, failed, TOPIC_FAILED, f"{product}:{native}", dedupe_id=f"failed:{ev.id}")
            conn.execute("INSERT INTO xw_processed VALUES(?,?,'parked')", (self.group, ev.id))
            return
        if res is not None:   # resolver path: the evidence travels with the event (panel, audit)
            top = res.candidates[0]
            ev = ev.model_copy(update={"data": {**ev.data, "identity": {
                "outcome": res.outcome, "confidence": res.confidence, "evidence": list(top.evidence),
                "notes": notes}}})
        outbox.add(conn, self._enrich(ev, canonical, entity), TOPIC_RESOLVED, canonical, dedupe_id=f"resolved:{ev.id}")
        conn.execute("INSERT INTO xw_processed VALUES(?,?,'resolved') ON CONFLICT(grp,event_id) DO UPDATE SET status='resolved'",
                     (self.group, ev.id))

    # --- loop ---------------------------------------------------------------------------------
    def run_once(self, limit: int = 100) -> int:
        n, last = 0, None
        for offset, ev in self.bus.poll(TOPIC_RAW, self.group, limit):
            with self.db.tx() as c:
                self._handle(c, ev)
            last, n = offset, n + 1
        self.relay.drain()
        if last is not None:
            self.bus.commit(TOPIC_RAW, self.group, last)  # after side effects are durable
        return n

    def drain(self) -> int:
        total = 0
        while n := self.run_once():
            total += n
        return total

    # --- operator fix flow ----------------------------------------------------------------------
    def apply_mapping(self, entry: CrosswalkEntry) -> int:
        """Add a mapping and republish parked events to events.raw (via the outbox, atomically)."""
        with self.db.tx() as c:
            parked = self.xw.resolve_in(c, entry)
            for ev in parked:
                outbox.add(c, ev, TOPIC_RAW, f"{PUBLISHER[ev.type]}:{entry.native_id}",
                           dedupe_id=f"republish:{ev.id}:{uuid.uuid4().hex[:8]}")
        self.relay.drain()
        return len(parked)
