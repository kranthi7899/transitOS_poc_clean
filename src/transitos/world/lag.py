"""How long each product takes to learn what is true (Phase 10, Step 3.2).

ASSUMED (A91). No source gives these; they are round placeholders for orders of magnitude: a telematics unit
reports within seconds, a shop rule or foreman acts within the half hour, a dispatcher notices and keys it in
within a quarter hour. Change them per scenario with ProductLag(...).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta


@dataclass(frozen=True)
class ProductLag:
    telematics_s: float = 5          # ASSUMED A91: telematics sees a fault / DVIR after this many seconds
    shop_rule_min: float = 20        # ASSUMED A91: the shop's rule creates (or closes) the maintenance record after this
    dispatcher_min: float = 12       # ASSUMED A91: the dispatcher flips scheduling status / records a call-out after this

    def __post_init__(self) -> None:
        for name in ("telematics_s", "shop_rule_min", "dispatcher_min"):
            v = getattr(self, name)
            if not (v >= 0):                              # also rejects NaN
                raise ValueError(f"{name} must be >= 0, got {v!r}")

    def delay(self, product: str) -> timedelta:
        if product == "telematics":
            return timedelta(seconds=self.telematics_s)
        if product == "maintenance":
            return timedelta(minutes=self.shop_rule_min)
        if product in ("scheduling", "workforce"):        # the dispatcher keys both in
            return timedelta(minutes=self.dispatcher_min)
        raise KeyError(f"no lag defined for product {product!r}")
