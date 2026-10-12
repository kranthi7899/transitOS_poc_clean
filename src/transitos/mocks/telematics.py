# ruff: noqa: B008
"""SIMULATED Samsara-style telematics: signed webhook + REST feed. Duplicates/lateness under chaos.

Delivery model: events get a `visible_at` (now, or now+30 sim-min under `late`). The webhook is
pushed when an event is emitted and re-attempted lazily on every request and on POST /sim/flush
(call it after advancing the clock). Under `duplicate` an event is delivered twice with the same eventId."""
from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import httpx
from fastapi import Body, Depends, FastAPI, Header, HTTPException

from .chaos import Chaos
from .clock import SimClock, iso, parse_ts
from .common import Ctx, make_app
from .seed import build_fleet

SECRET = b"dev-secret"  # A8
LATE_MINUTES = 30

Sender = Callable[[str, bytes, dict], None]


def sign(body: bytes, secret: bytes = SECRET) -> str:
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


def verify(body: bytes, signature: str, secret: bytes = SECRET) -> bool:
    return hmac.compare_digest(sign(body, secret), signature or "")


def encode(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def _ident(v: dict) -> dict:
    """Phase 10 Step 2: VIN read from the engine, licence plate and gateway install date, where the fleet has them."""
    out = {"vin": v.get("tele_vin"), "licensePlate": v.get("tele_plate"), "installedAt": v.get("tele_installed_at")}
    return {k: x for k, x in out.items() if x}


def _payload_vehicle(veh: dict) -> dict:
    return {k: veh[k] for k in ("id", "name", "vin", "licensePlate") if k in veh}


def _http_sender(url: str, body: bytes, headers: dict) -> None:
    httpx.post(url, content=body, headers=headers, timeout=5).raise_for_status()


def create_app(clock: SimClock, chaos: Chaos, db_path: Any = None, sender: Sender | None = None) -> FastAPI:
    ctx = Ctx("telematics", clock, chaos, db_path)
    st = ctx.store
    tenant = Depends(ctx.tenant)

    def seeder(a: str) -> None:
        fleet = build_fleet(a, clock.now())
        for v in fleet.vehicles:
            if v.get("tele") is None:   # Phase 10 Step 2: no telematics unit (e.g. a retired bus)
                continue
            st.put(a, "vehicles", v["tele"], {"id": v["tele"], "name": v.get("tele_name") or v["name"], **_ident(v)})
            if p := v.get("tele_prev"):   # a swapped-out gateway keeps its record, with the removal date
                st.put(a, "vehicles", p["id"], {"id": p["id"], "name": p.get("name") or v["name"],
                                                "removedAt": p["removed_at"],
                                                **({"vin": p["vin"]} if p.get("vin") else {})})

    app = make_app(ctx, "telematics", seeder)
    app.state.sender = sender or _http_sender

    def vehicle(a: str, vid: str) -> dict:
        v = st.get(a, "vehicles", vid)
        if v is None:
            raise HTTPException(404, "vehicle not found")
        return v

    def emit(a: str, payload: dict) -> None:
        n = st.next_seq(a, "event")
        late = chaos.on("late")
        visible = clock.now() + (timedelta(minutes=LATE_MINUTES) if late else timedelta())
        st.put(a, "events", f"{n:08d}", {
            "seq": n, "payload": payload, "visible_at": iso(visible),
            "dup": chaos.on("duplicate"), "delivered": False,
        })
        deliver_due(a)

    def deliver_due(a: str) -> int:
        hook = st.get(a, "config", "webhook")
        if not hook:
            return 0
        sent = 0
        now = clock.now()
        for e in sorted(st.all(a, "events"), key=lambda e: (e["visible_at"], e["seq"])):
            if e["delivered"] or parse_ts(e["visible_at"]) > now:
                continue
            body = encode(e["payload"])
            headers = {"X-Signature": sign(body), "X-Agency": a, "Content-Type": "application/json"}
            try:
                for _ in range(2 if e["dup"] else 1):
                    app.state.sender(hook["url"], body, headers)
                    sent += 1
            except Exception:  # noqa: BLE001,S112  receiver down: keep for the next attempt
                continue
            e["delivered"] = True
            st.put(a, "events", f"{e['seq']:08d}", e)
        return sent

    def fault_payload(a: str, veh: dict, spn: int, fmi: int, lamps: dict, count: int) -> dict:
        n = st.next_seq(a, "eventId")
        return {
            "eventId": f"evt-{n}", "vehicle": _payload_vehicle(veh),
            "fault": {"spn": spn, "fmi": fmi,
                      "lamps": {"stop": bool(lamps.get("stop")), "warn": bool(lamps.get("warn")), "protect": bool(lamps.get("protect"))},
                      "occurrenceCount": count},
            "time": ctx.wire_time(a),   # RFC 3339 UTC; epoch milliseconds under the Step 4.2 knob (A110)
        }

    @app.post("/sim/webhook")
    def set_webhook(body: dict = Body(...), x_agency: str = Header()):
        st.put(x_agency, "config", "webhook", {"url": body["url"]})
        return {"delivered": deliver_due(x_agency)}

    @app.post("/sim/flush")
    def flush(x_agency: str = Header()):
        return {"delivered": deliver_due(x_agency)}

    @app.post("/sim/fault")
    def sim_fault(body: dict = Body(...), x_agency: str = Header()):
        a = x_agency
        veh = vehicle(a, body["vehicle"])
        spn, fmi = int(body["spn"]), int(body["fmi"])
        key = f"{veh['id']}:{spn}:{fmi}"
        prior = st.get(a, "faults", key)
        count = (prior["fault"]["occurrenceCount"] + 1) if prior else 1
        payload = fault_payload(a, veh, spn, fmi, body.get("lamps") or {}, count)
        st.put(a, "faults", key, payload)
        emit(a, payload)
        return payload

    @app.post("/sim/fault/clear")
    def sim_clear(body: dict = Body(...), x_agency: str = Header()):
        st.delete(x_agency, "faults", f"{body['vehicle']}:{int(body['spn'])}:{int(body['fmi'])}")
        return {"ok": True}

    @app.get("/vehicles")
    def vehicles(a: str = tenant):   # Phase 10 Step 2: the unit roster (id, name, VIN, plate, install dates)
        return sorted(st.all(a, "vehicles"), key=lambda v: v["id"])

    @app.get("/faults/current")
    def current(vehicle: str | None = None, a: str = tenant):
        deliver_due(a)
        return [f for f in st.all(a, "faults") if not vehicle or f["vehicle"]["id"] == vehicle]

    @app.get("/faults/feed")
    def feed(cursor: str | None = None, a: str = tenant):
        deliver_due(a)
        after = int(cursor) if cursor else 0
        now = clock.now()
        items, last = [], after
        for e in st.all(a, "events"):
            if e["seq"] > after and parse_ts(e["visible_at"]) <= now:
                items += [e["payload"]] * (2 if e["dup"] else 1)
                last = max(last, e["seq"])
        return {"items": items, "next_cursor": str(last)}

    @app.post("/dvir")
    def dvir(body: dict = Body(...), a: str = tenant):
        veh = vehicle(a, body["vehicle"])
        n = st.next_seq(a, "eventId")
        payload = {
            "eventId": f"evt-{n}", "vehicle": _payload_vehicle(veh),
            "dvir": {"defect_text": body.get("defect_text", ""), "reported_by_role": body.get("reported_by_role", "operator")},
            "time": ctx.wire_time(a),   # A110
        }
        emit(a, payload)
        return {"eventId": payload["eventId"]}

    return app
