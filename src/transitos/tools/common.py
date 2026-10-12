"""Shared plumbing for adapters: HTTP with timeout/retry, dependency bundle, crosswalk shim."""
from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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

    def lookup(self, agency: str, product: str, native_id: str, at: datetime | None = None) -> str | None:
        """No validity stored (a test double): `at` is accepted (contract signature) and ignored (A147, A171)."""
        return self._by_native.get((agency, product, native_id))

    def all_ids(self, agency: str, canonical_id: str, at: datetime | None = None,
                entities: tuple[str, ...] | None = None) -> dict[str, str]:
        """No validity or entity is stored here (a test double): `at` and `entities` are accepted and ignored (A147)."""
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
    # Phase 10 Step 4: the agency's registry settings (tools/config/<agency>.yaml, plus any scenario patch), read
    # live: `timezone`, `local_time`, `maintenance.mode`. Empty = every default (A105, A108).
    settings_for: Callable[[str], dict] = field(default=lambda agency: {})


def settings(deps: Any, agency: str) -> dict:
    fn = getattr(deps, "settings_for", None)
    return (fn(agency) or {}) if callable(fn) else {}


def maintenance_mode(deps: Any, agency: str) -> str:
    """`work_order` (default: POST /workorders) or `pending_repair` (A105)."""
    return str(((settings(deps, agency).get("maintenance") or {}).get("mode")) or "work_order")


def local_time(deps: Any, agency: str) -> tuple[bool, str | None]:
    """(products send zone-less local time?, agency timezone). Default (False, None): products send zoned UTC."""
    s = settings(deps, agency)
    return bool(s.get("local_time")), s.get("timezone")


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


def native_of(deps: Deps, agency: str, canonical: str, product: str, *, operator: bool = False) -> str | None:
    """The canonical id's native id in `product`, valid now (G3, A147: the crosswalk filters by validity on the
    System clock and by entity); None = unknown. `operator=True` asks for an operator's (workforce) id."""
    if operator:
        return deps.crosswalk.all_ids(agency, canonical, entities=("operator",)).get(product)  # type: ignore[call-arg]
    return deps.crosswalk.all_ids(agency, canonical).get(product)


# ---- Phase 10 Step 4.2: zone-less local time ----------------------------------------------------------------
# A product that renders local wall-clock time with no zone (A108) is read with the AGENCY's timezone from
# tools/config/<agency>.yaml. Documented rules (A109), Review Focus 6:
#   * fall-back repeat (01:30 happens twice): the FIRST occurrence (fold=0) unless the caller passes fold=1 or the
#     product supplies a utc_offset for that field, which always wins (and must be one of the two real offsets);
#   * spring-forward gap (02:30 never shows on the clock): resolved FORWARD by the gap (02:30 -> 03:30 DST),
#     whatever the fold;
#   * a string that already carries a zone is a bug (mixed formats): ValueError, never silently accepted;
#   * a missing or unknown timezone is never guessed as UTC: UnknownTimezone (a ValueError).
# Known gap A112: without a utc_offset the second pass of the repeated hour is read an hour early (the rule's cost).

_LOCAL_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")
_LOCAL_HM = re.compile(r"^\d{2}:\d{2}$")
_ZONED = re.compile(r"(Z|[+-]\d{2}:?\d{2})$", re.IGNORECASE)
_OFFSET = re.compile(r"^([+-])(\d{2}):(\d{2})$")


class UnknownTimezone(ValueError):
    """The agency timezone is missing or not a zoneinfo key: zone-less local time cannot be read (never UTC)."""


class ZoneConfigMismatch(ValueError):
    """Bug sweep B fix round (N1, A165): the agency's time setting does not match what the product sends (zone-less
    time with the local_time flag off, or a zoned string with it on). Configuration, not one bad record."""


def zone(tz: str | None) -> ZoneInfo:
    if not isinstance(tz, str) or not tz.strip():
        raise UnknownTimezone("agency timezone is not configured: zone-less local time cannot be read")
    try:
        return ZoneInfo(tz.strip())
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise UnknownTimezone(f"agency timezone {tz!r} is not a known zone") from e


def _offset(s: str) -> timedelta:
    m = _OFFSET.match(s.strip()) if isinstance(s, str) else None
    if m is None:
        raise ValueError(f"utc_offset {s!r} is not +HH:MM / -HH:MM")
    sign = -1 if m.group(1) == "-" else 1
    return sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))


def to_utc(naive: str, tz: str, *, fold: int = 0, utc_offset: str | None = None,
           on: date | None = None) -> datetime:
    """Zone-less local `YYYY-MM-DDTHH:MM:SS` (or `HH:MM` on the date `on`) in `tz` -> aware UTC datetime (A109)."""
    if fold not in (0, 1):
        raise ValueError("fold must be 0 or 1")
    s = naive.strip() if isinstance(naive, str) else ""
    if s and _ZONED.search(s) and not _LOCAL_HM.match(s):
        raise ZoneConfigMismatch(f"timestamp {naive!r} already carries a zone: mixed formats are a bug (A109)")
    zi = zone(tz)
    if _LOCAL_HM.match(s):
        if on is None:
            raise ValueError(f"HH:MM {naive!r} needs a date")
        s = f"{on.isoformat()}T{s}:00"
    if not _LOCAL_TS.match(s):
        raise ValueError(f"not a zone-less local timestamp YYYY-MM-DDTHH:MM:SS: {naive!r}")
    wall = datetime.fromisoformat(s)      # naive; raises on an impossible date or hour
    if utc_offset is not None:
        inst = (wall - _offset(utc_offset)).replace(tzinfo=UTC)
        if inst.astimezone(zi).replace(tzinfo=None) != wall:
            raise ValueError(f"utc_offset {utc_offset} does not fit {naive!r} in {tz}: product and agency zone disagree")
        return inst
    a = wall.replace(tzinfo=zi, fold=0).astimezone(UTC)
    b = wall.replace(tzinfo=zi, fold=1).astimezone(UTC)
    if a.astimezone(zi).replace(tzinfo=None) != wall:      # a gap: no instant shows this wall time
        # fold=0 applies the offset in force BEFORE the transition, which lands the reading forward by the gap
        return max(a, b)
    return b if fold == 1 else a


def payload_time_reader(cfg: dict) -> Callable[[dict, str], datetime] | None:
    """For a watch adapter: read `doc[key]` as zone-less local time in the agency timezone (the product's optional
    per-field `utc_offset` map disambiguates), or None when the agency's products send zoned time (the default)."""
    if not cfg.get("local_time"):
        return None
    tz = cfg.get("timezone")

    def read(doc: dict, key: str) -> datetime:
        offs = doc.get("utc_offset") if isinstance(doc.get("utc_offset"), dict) else {}
        return to_utc(doc[key], tz, utc_offset=offs.get(key))  # type: ignore[arg-type]
    return read


def epoch_ms_to_utc(ms: int) -> datetime:
    """Epoch milliseconds (telematics wire format under the local-time knob, A110) -> aware UTC datetime."""
    if isinstance(ms, bool) or not isinstance(ms, int):
        raise ValueError(f"epoch milliseconds must be an integer, got {ms!r}")  # noqa: TRY004 - a bad payload value
    # fix round F3 (A146): plausibility range 2000-01-01 (incl.) .. 2100-01-01 (excl.). Epoch SECONDS, nanoseconds,
    # 0 and negatives are refused, never read as 1970 or as an overflow
    if not EPOCH_MS_MIN <= ms < EPOCH_MS_MAX:
        raise ValueError(f"epoch milliseconds {ms} outside 2000-01-01..2100-01-01 (seconds or nanoseconds?)")
    try:
        return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=ms)
    except OverflowError as e:   # unreachable inside the range; kept so an overflow is always a ValueError
        raise ValueError(f"epoch milliseconds {ms} out of range") from e


EPOCH_MS_MIN, EPOCH_MS_MAX = 946_684_800_000, 4_102_444_800_000
