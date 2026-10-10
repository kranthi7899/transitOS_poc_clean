"""Every read returns ok | partial | unavailable, never a guess (design §4.4)."""
from __future__ import annotations

from typing import Generic, Literal, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class Result(BaseModel, Generic[T]):
    status: Literal["ok", "partial", "unavailable"]
    value: T | None = None
    product: str | None = None
    note: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @classmethod
    def success(cls, value: T, note: str | None = None) -> "Result[T]":
        return cls(status="ok", value=value, note=note)

    @classmethod
    def unavailable(cls, product: str, note: str | None = None) -> "Result[T]":
        return cls(status="unavailable", product=product, note=note)

    @classmethod
    def part(cls, value: T, product: str, note: str | None = None) -> "Result[T]":
        return cls(status="partial", value=value, product=product, note=note)
