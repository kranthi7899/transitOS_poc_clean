"""Clearance item (v1.2, request C-1): one rule's verdict on one option, with the basis of the data behind it.

Missing data is never a violation and never a silent pass: it surfaces as `unverifiable` (safety/legal rules) or
`not_evaluated` (optional rules). Everything that is not `native` is shown in the plan and the decision record."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

ClearanceStatus = Literal["pass", "warn", "unverifiable", "not_evaluated", "violation"]
Basis = Literal["native", "derived", "assumed", "unknown"]


class ClearanceItem(BaseModel):
    rule: str
    status: ClearanceStatus
    basis: Basis
    detail: str = ""
    requires_ack: bool = False
