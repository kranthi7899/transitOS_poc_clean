"""Write-side wrapper the workflow sees as `tools.writes`: forwards every write to the real Track C
ToolSet and keeps a write log (one `WriteRecord` per call that reached the adapter) with a global
sequence number shared with the integration audit events. Test hooks let the verification World inject
per-action chaos without touching adapters or mocks."""
from __future__ import annotations

import threading
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from typing import Any

from transitos.contracts import ids
from transitos.contracts.results import Result
from transitos.verification.model import WriteRecord


class Seq:
    """Global monotonically increasing sequence (writes, approvals, kill-switch toggles, audit)."""

    def __init__(self) -> None:
        self._n = 0
        self._lock = threading.Lock()

    def next(self) -> int:
        with self._lock:
            self._n += 1
            return self._n


def _params(action: str, a: tuple, kw: dict) -> dict[str, Any]:
    names = {
        "create_work_order": ("vehicle_id", "description"),
        "cancel_work_order": ("wo_id",),
        "set_vehicle_unavailable": ("vehicle_id", "start", "end"),
        "set_vehicle_available": ("vehicle_id",),
        "reassign_block": ("block_id", "to_vehicle_id"),
        "assign_operator": ("block_id", "operator_id"),
        "publish_rider_alert": ("block_id", "message"),
    }[action]
    out = {}
    for n, v in zip(names, a[1:]):  # a[0] is the agency
        out[n] = v.isoformat() if hasattr(v, "isoformat") else v
    return out


def _effect(action: str, res: Result) -> str | None:
    v = res.value
    if v is None:
        return None
    if action in ("create_work_order", "cancel_work_order"):
        return v.id
    if action in ("set_vehicle_unavailable", "set_vehicle_available"):
        return f"{v.id}:{v.status}"
    if action == "reassign_block":
        return f"{v.id}->{v.vehicle_id}"
    if action == "assign_operator":
        return f"{v.id}->{v.operator_id}"
    if action == "publish_rider_alert":
        return v.get("alert_id")
    return None


class WriteLog:
    def __init__(self, seq: Seq, run_ids: Callable[[], list[str]]):
        self.seq, self.run_ids = seq, run_ids
        self.records: list[WriteRecord] = []
        self._lock = threading.Lock()
        # hooks (verification / demo); all optional
        # around(action, key, compensation) -> context manager wrapped around each adapter call
        self.around: Callable[[str, str, bool], AbstractContextManager] = lambda *_: nullcontext()
        self.after: Callable[[WriteRecord, Result], None] | None = None
        self.repeat: Callable[[str], int] = lambda action: 1                  # calls per write (retry chaos)
        self.crash_before: Callable[[str, str], None] | None = None           # may raise (process-kill sim)

    def _run_of(self, key: str) -> str:
        cands = [r for r in self.run_ids() if key.startswith(r + ":")]
        return max(cands, key=len) if cands else key.rsplit(":", 2)[0]

    def call(self, action: str, fn: Callable[..., Result], a: tuple, kw: dict) -> Result:
        key = a[-1]
        run_id = self._run_of(key)
        orig = ids.compensated_key(run_id, key)
        comp = orig is not None
        if self.crash_before:
            self.crash_before(action, key)
        res: Result = Result.unavailable("tools", "not called")
        for _ in range(max(1, self.repeat(action) if not comp else 1)):
            with self.around(action, key, comp):
                res = fn(*a, **kw)
            rec = WriteRecord(
                seq=self.seq.next(), run_id=run_id, action=action, key=key,
                plan_version=int(key.rsplit(":", 1)[1]) if key.rsplit(":", 1)[-1].isdigit() else 1,
                params=_params(action, a, kw), ok=res.status in ("ok", "partial"),
                effect_id=_effect(action, res), compensation=comp, compensates_key=orig)
            with self._lock:
                self.records.append(rec)
            if self.after:
                self.after(rec, res)
        return res


class RecordingWrites:
    """WriteTools over a Track C ToolSet, logging each call."""

    def __init__(self, toolset: Any, log: WriteLog):
        self._t, self._log = toolset, log

    def _w(self, action: str, *a: Any, **kw: Any) -> Result:
        return self._log.call(action, getattr(self._t, action), a, kw)

    def create_work_order(self, *a, **kw):
        return self._w("create_work_order", *a, **kw)

    def cancel_work_order(self, *a, **kw):
        return self._w("cancel_work_order", *a, **kw)

    def set_vehicle_unavailable(self, *a, **kw):
        return self._w("set_vehicle_unavailable", *a, **kw)

    def set_vehicle_available(self, *a, **kw):
        return self._w("set_vehicle_available", *a, **kw)

    def reassign_block(self, *a, **kw):
        return self._w("reassign_block", *a, **kw)

    def assign_operator(self, *a, **kw):
        return self._w("assign_operator", *a, **kw)

    def publish_rider_alert(self, *a, **kw):
        return self._w("publish_rider_alert", *a, **kw)


class WorkflowTools:
    """The ToolSet-like bundle Track D's graph expects: `.reads` / `.writes`."""

    def __init__(self, toolset: Any, log: WriteLog):
        self.reads = toolset
        self.writes = RecordingWrites(toolset, log)
