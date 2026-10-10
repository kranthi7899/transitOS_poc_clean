"""Every read returns ok | partial | unavailable, never a guess (design §4.4)."""
from __future__ import annotations

from typing import Generic, Literal, TypeVar

from pydantic import BaseModel

T = TypeVar("T")

# A write the adapter refused to send because policy forbids it (not a product outage):
#   Result(status="unavailable", product=POLICY, note="kill_switch: ..." | "hard_limit: ...")
# Kept as a convention on the existing status (no new enum value, so old consumers still treat it
# as "did not happen"); use Result.blocked(...) / .is_blocked rather than matching strings.
POLICY = "policy"
KILL_SWITCH = "kill_switch"
HARD_LIMIT = "hard_limit"


class Result(BaseModel, Generic[T]):
    status: Literal["ok", "partial", "unavailable"]
    value: T | None = None
    product: str | None = None
    note: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def is_blocked(self) -> bool:
        """Write refused by policy (kill switch or hard limit). Never retry it."""
        return self.status == "unavailable" and self.product == POLICY

    @property
    def blocked_reason(self) -> str | None:
        """'kill_switch' | 'hard_limit' for a blocked write, else None."""
        if not self.is_blocked:
            return None
        return KILL_SWITCH if (self.note or "").startswith(KILL_SWITCH) else HARD_LIMIT

    @classmethod
    def blocked(cls, reason: str, detail: str = "") -> Result[T]:
        """reason: KILL_SWITCH or HARD_LIMIT. note = '<reason>: <detail>'."""
        if reason not in (KILL_SWITCH, HARD_LIMIT):
            raise ValueError(f"blocked reason must be {KILL_SWITCH!r} or {HARD_LIMIT!r}")
        return cls(status="unavailable", product=POLICY, note=f"{reason}: {detail}" if detail else reason)

    @classmethod
    def success(cls, value: T, note: str | None = None) -> Result[T]:
        return cls(status="ok", value=value, note=note)

    @classmethod
    def unavailable(cls, product: str, note: str | None = None) -> Result[T]:
        return cls(status="unavailable", product=product, note=note)

    @classmethod
    def part(cls, value: T, product: str, note: str | None = None) -> Result[T]:
        return cls(status="partial", value=value, product=product, note=note)
