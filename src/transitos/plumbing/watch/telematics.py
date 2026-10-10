"""Telematics: HMAC-verified webhook receiver + backfill poller, reconciled by vendor eventId."""
from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from typing import Any

from fastapi import APIRouter, FastAPI, Request, Response

from transitos.contracts import ids

from .. import outbox
from ..db import Db
from .base import Poller, load_state, make_event, raw_key, save_state

SEEN_SCHEMA = """
CREATE TABLE IF NOT EXISTS telematics_seen(
  agency TEXT NOT NULL, vendor_event_id TEXT NOT NULL, event_id TEXT NOT NULL,
  PRIMARY KEY(agency, vendor_event_id));
"""


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def ingest_payload(conn: sqlite3.Connection, agency: str, p: dict[str, Any]) -> bool:
    """Map one vendor payload to a raw event inside the caller's transaction. False if already seen."""
    vendor_id = p["eventId"]
    if conn.execute("SELECT 1 FROM telematics_seen WHERE agency=? AND vendor_event_id=?", (agency, vendor_id)).fetchone():
        return False
    veh = p["vehicle"]["id"]
    if "defect" in p:  # DVIR (assumed A13: DVIRs arrive on the same webhook/feed with a `defect` object)
        d = p["defect"]
        text = d.get("text", "")
        ev = make_event(
            agency, "telematics",
            ids.defect_id(agency, veh, p["time"], hashlib.sha1(text.encode()).hexdigest()[:12]),
            "DefectReported", p["time"],
            {"native_vehicle_id": veh, "defect_text": text, "reported_by_role": d.get("reportedByRole", "operator")})
    else:
        f = p["fault"]
        lamps = f.get("lamps", {})
        ev = make_event(
            agency, "telematics", ids.fault_id(agency, veh, f["spn"], f["fmi"], p["time"]),
            "VehicleFaultDetected", p["time"],
            {"native_vehicle_id": veh, "spn": f["spn"], "fmi": f["fmi"],
             "lamps": {k: bool(lamps.get(k, False)) for k in ("stop", "warn", "protect")},
             "occurrence_count": f.get("occurrenceCount", 1), "source": "sensor"})
    conn.execute("INSERT INTO telematics_seen VALUES(?,?,?)", (agency, vendor_id, ev.id))
    outbox.add(conn, ev, "events.raw", raw_key("telematics", veh))
    return True


class WebhookReceiver:
    def __init__(self, db: Db, agency: str, secret: str = "dev-secret"):
        self.db, self.agency, self.secret = db, agency, secret
        db.script(SEEN_SCHEMA)
        outbox.ensure_schema(db)

    def handle(self, body: bytes, signature: str | None) -> tuple[int, dict]:
        if not signature or not hmac.compare_digest(signature, sign(self.secret, body)):
            return 401, {"error": "bad signature"}
        try:
            payload = json.loads(body)
            with self.db.tx() as c:
                new = ingest_payload(c, self.agency, payload)
        except (KeyError, ValueError, TypeError) as e:
            return 400, {"error": f"malformed payload: {e!r}"}
        return 200, {"status": "accepted" if new else "duplicate"}

    def router(self) -> APIRouter:
        r = APIRouter()

        @r.post("/webhook")
        async def webhook(request: Request) -> Response:
            code, body = self.handle(await request.body(), request.headers.get("X-Signature"))
            return Response(json.dumps(body), status_code=code, media_type="application/json")

        return r

    def app(self) -> FastAPI:
        a = FastAPI()
        a.include_router(self.router())
        return a


class TelematicsBackfillPoller(Poller):
    """GET /faults/feed?cursor= reconciled against the webhook by vendor eventId (no double events)."""

    product = "telematics"
    name = "telematics-backfill"

    def __init__(self, db: Db, client, agency: str):
        super().__init__(db, client, agency)
        self.name = f"telematics-backfill:{agency}"
        db.script(SEEN_SCHEMA)

    def poll(self) -> int:
        import httpx

        cursor, _ = load_state(self.db, self.name)
        try:
            data = self.get("/faults/feed", cursor=cursor)
        except httpx.HTTPError as e:
            self.last_error = repr(e)
            return 0
        self.last_error = None
        added = 0
        with self.db.tx() as c:
            for item in data.get("items", []):
                added += ingest_payload(c, self.agency, item)
            if self.before_commit:
                self.before_commit()
            save_state(c, self.name, data.get("next_cursor") or cursor, {})
        return added
