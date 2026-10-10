"""Shared plumbing for adapters: HTTP with timeout/retry, dependency bundle, crosswalk shim."""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from transitos.contracts.crosswalk import Crosswalk, CrosswalkEntry
from transitos.contracts.policy import AgencyPolicy
from transitos.contracts.results import Result

from .idem_store import IdemStore
from .policy import KillSwitchStore, default_store, load_policy

TIMEOUT_S = 3.0
RETRY_STATUS = {429, 500, 502, 503, 504}


class ProductUnavailable(Exception):
    def __init__(self, product: str, note: str):
        super().__init__(f"{product}: {note}")
        self.product = product
        self.note = note


class StaticCrosswalk:
    """Minimal in-memory Crosswalk for tests / local use (Track B owns the real one).
    Only lookup/all_ids are used by adapters."""

    def __init__(self) -> None:
        self._by_native: dict[tuple[str, str, str], str] = {}
        self._by_canonical: dict[tuple[str, str], dict[str, str]] = {}

    def add(self, agency: str, canonical_id: str, **natives: str) -> None:
        """add('a1', 'veh-1', scheduling='BUS-44', maintenance='1106', telematics='T-77')"""
        for product, native in natives.items():
            self._by_native[(agency, product, native)] = canonical_id
            self._by_canonical.setdefault((agency, canonical_id), {})[product] = native

    def lookup(self, agency: str, product: str, native_id: str) -> str | None:
        return self._by_native.get((agency, product, native_id))

    def all_ids(self, agency: str, canonical_id: str) -> dict[str, str]:
        return dict(self._by_canonical.get((agency, canonical_id), {}))

    def add_unresolved(self, agency: str, product: str, native_id: str, event_id: str) -> None:
        pass

    def resolve(self, entry: CrosswalkEntry) -> list:
        self.add(entry.agency, entry.canonical_id, **{entry.product: entry.native_id})
        return []


@dataclass
class Deps:
    clients: dict[str, httpx.Client]
    crosswalk: Crosswalk
    idem: IdemStore = field(default_factory=IdemStore)
    killswitch: KillSwitchStore = field(default_factory=default_store)
    policy_for: Callable[[str], AgencyPolicy] = load_policy
    sleep: Callable[[float], None] = time.sleep
    retries: int = 3
    backoff_s: float = 0.2


class Http:
    """One product's HTTP access. Adds X-Agency, 3s timeout, retry w/ exponential backoff on 429/5xx
    and transport errors. Raises ProductUnavailable when retries are exhausted."""

    def __init__(self, product: str, deps: Deps):
        self.product = product
        self.deps = deps

    def request(self, agency: str, method: str, path: str, *, ok_4xx: bool = False, **kw: Any) -> httpx.Response:
        client = self.deps.clients.get(self.product)
        if client is None:
            raise ProductUnavailable(self.product, "no client configured")
        headers = {"X-Agency": agency, **kw.pop("headers", {})}
        last = "unknown"
        for attempt in range(self.deps.retries + 1):
            try:
                # timeout is a client setting (Registry builds httpx.Client(timeout=TIMEOUT_S); injected
                # clients bring their own) - Starlette's in-process TestClient rejects per-request timeouts
                resp = client.request(method, path, headers=headers, **kw)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                last = f"transport error: {type(e).__name__}"
            else:
                if resp.status_code not in RETRY_STATUS:
                    if resp.status_code >= 400 and not ok_4xx:
                        raise ProductUnavailable(self.product, f"http {resp.status_code}: {resp.text[:120]}")
                    return resp
                last = f"http {resp.status_code}"
            if attempt < self.deps.retries:
                self.deps.sleep(self.deps.backoff_s * (2 ** attempt))
        raise ProductUnavailable(self.product, f"unavailable after retries ({last})")

    def json(self, agency: str, method: str, path: str, **kw: Any) -> Any:
        return self.request(agency, method, path, **kw).json()


def unavailable(product: str, note: str) -> Result:
    return Result.unavailable(product, note)


def native_of(deps: Deps, agency: str, canonical: str, product: str) -> str | None:
    return deps.crosswalk.all_ids(agency, canonical).get(product)
