"""Atomic per-product snapshot persistence for the connected SIMULATED profile.

Each request stages all native Store mutations and its idempotency response in one
Delta row update. Baseline import tables remain untouched. Intended for one local
writer per product/instance; revision checks reject a competing writer.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
from uuid import uuid4

from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .clock import SimClock, parse_ts
from .databricks_maintenance import Settings, Warehouse, WarehouseUnavailable
from .store import Store

PRODUCT_SCHEMAS = {"telematics": "telematics", "maintenance": "maintenance", "scheduling": "scheduling",
                   "workforce": "workforce", "alerts": "scheduling"}


def state_table(settings: Settings, product: str) -> str:
    return f"`{settings.catalog}`.`{PRODUCT_SCHEMAS[product]}`.`api_state`"


def encode(docs: dict) -> str:
    # Double-encode individual documents for a simple MAP<STRING,MAP<STRING,STRING>> SQL view.
    return json.dumps({coll: {key: json.dumps(doc, sort_keys=True) for key, doc in rows.items()}
                       for coll, rows in docs.items()}, sort_keys=True)


def decode(body: str) -> dict:
    return {coll: {key: json.loads(doc) for key, doc in rows.items()}
            for coll, rows in json.loads(body).items()}


class RevisionConflict(Exception):
    pass


class ProductState(Store):
    """Store API, with the unit of durability being the entire native API request."""

    def __init__(self, warehouse: Warehouse, product: str, instance: str, agency: str, clock: SimClock):
        if product not in PRODUCT_SCHEMAS or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", instance):
            raise ValueError("Invalid product/instance")
        self.warehouse, self.product, self.instance, self.agency, self.clock = warehouse, product, instance, agency, clock
        self.table = state_table(warehouse.settings, product)
        self.docs, self.original, self.revision = {}, "", 0
        self.active = False

    def load(self):
        rows = self.warehouse.query(
            f"SELECT body, revision, clock_at, commit_token FROM {self.table} "
            "WHERE instance_id = ? AND agency = ? AND product = ? AND is_simulated = TRUE",
            (self.instance, self.agency, self.product))
        if len(rows) != 1:
            raise WarehouseUnavailable
        row = rows[0]
        try:
            self.docs, self.original, self.revision = decode(row["body"]), row["body"], row["revision"]
            self.clock._now = parse_ts(row["clock_at"])
        except (TypeError, ValueError, KeyError, AttributeError):
            raise WarehouseUnavailable from None
        self.original_clock = self.clock.iso()
        self.active = True

    def commit(self):
        body, at = encode(self.docs), self.clock.iso()
        if body == self.original and at == self.original_clock:
            return
        token = uuid4().hex
        self.warehouse.query(
            f"UPDATE {self.table} SET body = ?, clock_at = ?, revision = revision + 1, commit_token = ? "
            "WHERE instance_id = ? AND agency = ? AND product = ? AND is_simulated = TRUE AND revision = ?",
            (body, at, token, self.instance, self.agency, self.product, self.revision))
        rows = self.warehouse.query(
            f"SELECT commit_token FROM {self.table} WHERE instance_id = ? AND agency = ? "
            "AND product = ? AND is_simulated = TRUE", (self.instance, self.agency, self.product))
        if len(rows) != 1 or rows[0]["commit_token"] != token:
            raise RevisionConflict

    def _check(self, agency):
        if not self.active or agency != self.agency:
            raise ValueError("No active product request for that agency")

    def put(self, agency, coll, id, doc):
        self._check(agency)
        self.docs.setdefault(coll, {})[id] = copy.deepcopy(doc)

    def get(self, agency, coll, id):
        self._check(agency)
        return copy.deepcopy(self.docs.get(coll, {}).get(id))

    def all(self, agency, coll):
        self._check(agency)
        return copy.deepcopy(list(self.docs.get(coll, {}).values()))

    def delete(self, agency, coll, id):
        self._check(agency)
        self.docs.get(coll, {}).pop(id, None)

    def clear(self, agency=None):
        raise ValueError("Connected product reset/seeding is disabled; use a new instance")

    def next_seq(self, agency, name):
        doc = self.get(agency, "_seq", name) or {"n": 0}
        doc["n"] += 1
        self.put(agency, "_seq", name, doc)
        return doc["n"]


class AtomicProductMiddleware:
    def __init__(self, app, store: ProductState):
        self.app, self.store, self.lock = app, store, asyncio.Lock()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path, method = scope["path"], scope["method"]
        if path in ("/docs", "/openapi.json", "/docs/oauth2-redirect", "/redoc", "/health"):
            return await self.app(scope, receive, send)
        if path in ("/sim/reset", "/sim/seed", "/sim/chaos", "/sim/webhook"):
            return await JSONResponse({"detail": "Disabled in connected profile"}, 409)(scope, receive, send)
        headers = dict(scope["headers"])
        agency = headers.get(b"x-agency", b"").decode()
        if agency != self.store.agency and path != "/sim/clock":
            return await JSONResponse({"detail": "Configured synthetic agency required"}, 400)(scope, receive, send)
        raw = b""
        while True:
            event = await receive()
            if event["type"] == "http.disconnect":
                return
            raw += event.get("body", b"")
            if len(raw) > 1_000_000:
                return await JSONResponse({"detail": "Request too large"}, 413)(scope, receive, send)
            if not event.get("more_body"):
                break
        delivered = False

        async def replay_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": raw, "more_body": False}
            return {"type": "http.disconnect"}

        async with self.lock:
            try:
                await run_in_threadpool(self.store.load)
                key = headers.get(b"idempotency-key") if method == "POST" else None
                digest = hashlib.sha256(key).hexdigest() if key else None
                fingerprint = hashlib.sha256(method.encode() + path.encode() + raw
                                             + headers.get(b"x-restore", b"")).hexdigest()
                prior = self.store.get(self.store.agency, "_requests", digest) if digest else None
                if prior:
                    if prior["fingerprint"] != fingerprint:
                        return await JSONResponse({"detail": "Idempotency key reused with different request"}, 409)(
                            scope, replay_receive, send)
                    if not (self.store.product == "maintenance" and path == "/workorders"):
                        await send({"type": "http.response.start", "status": prior["status"],
                                    "headers": [(k.encode(), v.encode()) for k, v in prior["headers"]]})
                        return await send({"type": "http.response.body", "body": prior["body"].encode()})
                    # Native maintenance replay deliberately returns the order's CURRENT state.
                    # Its own idem document and order were committed together in this same snapshot.
                messages = []

                async def buffered(message):
                    messages.append(message)

                await self.app(scope, replay_receive, buffered)
                start = next(message for message in messages if message["type"] == "http.response.start")
                if start["status"] < 400:
                    if digest:
                        body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
                        self.store.put(self.store.agency, "_requests", digest, {
                            "fingerprint": fingerprint, "status": start["status"], "body": body.decode(),
                            "headers": [(k.decode(), v.decode()) for k, v in start["headers"]]})
                    await run_in_threadpool(self.store.commit)
                for message in messages:
                    await send(message)
            except (WarehouseUnavailable, RevisionConflict):
                await JSONResponse({"detail": "Cloud state unavailable/conflicted; retry with the same key. "
                                    "No local fallback."}, 503)(scope, replay_receive, send)
            finally:
                self.store.active = False
