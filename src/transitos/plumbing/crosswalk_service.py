"""events.raw -> (crosswalk) -> events.resolved keyed by canonical vehicle ID; unknown IDs park + events.failed.

Exactly-once effect on top of at-least-once: processing, processed-marker and outbox rows share one transaction;
the relay publishes afterwards. Raw replay is a no-op for events already resolved.
"""
from __future__ import annotations

import uuid

from transitos.contracts.crosswalk import CrosswalkEntry
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
    def __init__(self, db: Db, bus: SqliteBus | KafkaBus, crosswalk: SqliteCrosswalk, group: str = GROUP):
        self.db, self.bus, self.xw, self.group = db, bus, crosswalk, group
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

    def _handle(self, conn, ev: CloudEvent) -> None:
        row = conn.execute("SELECT status FROM xw_processed WHERE grp=? AND event_id=?", (self.group, ev.id)).fetchone()
        if row and row["status"] == "resolved":
            return
        field, entity = NATIVE_FIELD[ev.type]
        product = PUBLISHER[ev.type]
        native = str(ev.data[field])
        canonical = self.xw.lookup(ev.agencyid, product, native)
        if canonical is None:
            if row:  # already parked and reported; wait for resolve()
                return
            self.xw.add_unresolved_in(conn, ev.agencyid, product, native, ev)
            failed = ev.model_copy(update={"failurereason": f"unresolved {entity} {product}:{native}"})
            outbox.add(conn, failed, TOPIC_FAILED, f"{product}:{native}", dedupe_id=f"failed:{ev.id}")
            conn.execute("INSERT INTO xw_processed VALUES(?,?,'parked')", (self.group, ev.id))
            return
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
