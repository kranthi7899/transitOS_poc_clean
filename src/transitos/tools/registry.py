"""Per-agency tool registry. config/<agency>.yaml maps tool -> adapter id; swapping a product is a
one-line config change (point the tool at another registered adapter id)."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

from transitos.contracts.crosswalk import Crosswalk
from transitos.contracts.objects import Block, Needs, Operator, Vehicle, WorkOrder
from transitos.contracts.results import Result
from transitos.contracts.tools import SAGA_ORDER

from .common import TIMEOUT_S, Deps, ProductUnavailable, StaticCrosswalk
from .idem_store import IdemStore
from .policy import CONFIG_DIR, KillSwitchStore, default_store, load_policy
from .read_adapters import MaintenanceReader, SchedulingReader, WorkforceReader
from .write_adapters import (
    AlertsWriter,
    MaintenanceWriter,
    SchedulingWriter,
    WorkforceWriter,
)

READ_TOOLS = ["get_vehicle", "get_vehicle_history", "list_affected_blocks", "list_ready_vehicles",
              "list_eligible_operators", "find_open_work_order", "get_work_order"]
WRITE_TOOLS = ["create_work_order", "cancel_work_order", "set_vehicle_unavailable", "set_vehicle_available",
               "reassign_block", "assign_operator", "publish_rider_alert"]


# A read adapter and a write adapter for one product, exposed as a single adapter id.
class _Composite:
    def __init__(self, *parts: Any):
        self._parts = parts

    def __getattr__(self, name: str) -> Any:
        for p in self._parts:
            if not name.startswith("_") and hasattr(p, name):
                return getattr(p, name)
        raise AttributeError(name)


AdapterFactory = Callable[[Deps], Any]

BUILTIN_ADAPTERS: dict[str, AdapterFactory] = {
    "maintenance_mock_v1": lambda d: _Composite(MaintenanceReader(d), MaintenanceWriter(d)),
    "scheduling_mock_v1": lambda d: _Composite(SchedulingReader(d), SchedulingWriter(d)),
    "workforce_mock_v1": lambda d: _Composite(WorkforceReader(d), WorkforceWriter(d)),
    "alerts_mock_v1": lambda d: AlertsWriter(d),
}


class ToolSet:
    """Implements ReadTools + WriteTools for one agency by delegating each tool to its configured adapter."""

    def __init__(self, agency: str, impl: dict[str, Callable[..., Result]], adapter_ids: dict[str, str],
                 adapters: list[Any] | None = None):
        missing = [t for t in READ_TOOLS + WRITE_TOOLS if t not in impl]
        if missing:
            raise ValueError(f"registry for {agency} missing tools: {missing}")
        self.agency = agency
        self._impl = impl
        self.adapter_ids = adapter_ids
        self._adapters = adapters or []

    # ---- optional reads (Phase 10): not in ReadTools; supplied by whichever adapter offers them -------
    def _optional(self, name: str) -> list[Callable[..., Any]]:
        return [fn for a in self._adapters if callable(fn := getattr(a, name, None))]

    def list_blocks(self, agency: str, start: datetime, end: datetime) -> Result[list[Block] | None]:
        """Every scheduled block in the window (the feed double-booking is judged against). A None value means
        no configured adapter offers it (the clearance rule then fails closed)."""
        fns = self._optional("list_blocks")
        if not fns:
            return Result.success(None, note="not_supported")
        return fns[0](agency, start, end)

    def capability_profiles(self, agency: str) -> Result[dict[str, dict]]:
        """What each configured product supplies, as {product: CapabilityProfile.to_dict()}."""
        out: dict[str, dict] = {}
        try:
            for fn in self._optional("capability_profile"):
                p = fn(agency)
                out[p.product] = p.to_dict()
        except ProductUnavailable as e:
            return Result.unavailable(e.product, e.note)
        return Result.success(out)

    def _call(self, tool: str, agency: str, *a: Any, **kw: Any) -> Result:
        if agency != self.agency:
            raise ValueError(f"tool set for {self.agency} called with agency {agency}")
        return self._impl[tool](agency, *a, **kw)

    # reads
    def get_vehicle(self, agency: str, vehicle_id: str) -> Result[Vehicle]:
        return self._call("get_vehicle", agency, vehicle_id)

    def get_vehicle_history(self, agency: str, vehicle_id: str) -> Result[list[dict]]:
        return self._call("get_vehicle_history", agency, vehicle_id)

    def list_affected_blocks(self, agency: str, vehicle_id: str, start: datetime, end: datetime) -> Result[list[Block]]:
        return self._call("list_affected_blocks", agency, vehicle_id, start, end)

    def list_ready_vehicles(self, agency: str, start: datetime, end: datetime, needs: Needs) -> Result[list[Vehicle]]:
        return self._call("list_ready_vehicles", agency, start, end, needs)

    def list_eligible_operators(self, agency: str, block: Block) -> Result[list[Operator]]:
        return self._call("list_eligible_operators", agency, block)

    def find_open_work_order(self, agency: str, vehicle_id: str) -> Result[WorkOrder | None]:
        return self._call("find_open_work_order", agency, vehicle_id)

    def get_work_order(self, agency: str, wo_id: str) -> Result[WorkOrder]:
        return self._call("get_work_order", agency, wo_id)

    # writes
    def create_work_order(self, agency: str, vehicle_id: str, description: str, key: str) -> Result[WorkOrder]:
        return self._call("create_work_order", agency, vehicle_id, description, key)

    def cancel_work_order(self, agency: str, wo_id: str, key: str) -> Result[WorkOrder]:
        return self._call("cancel_work_order", agency, wo_id, key)

    def set_vehicle_unavailable(self, agency: str, vehicle_id: str, start: datetime, end: datetime | None,
                                key: str) -> Result[Vehicle]:
        return self._call("set_vehicle_unavailable", agency, vehicle_id, start, end, key)

    def set_vehicle_available(self, agency: str, vehicle_id: str, key: str) -> Result[Vehicle]:
        return self._call("set_vehicle_available", agency, vehicle_id, key)

    def reassign_block(self, agency: str, block_id: str, to_vehicle_id: str, key: str, **kw: Any) -> Result[Block]:
        return self._call("reassign_block", agency, block_id, to_vehicle_id, key, **kw)

    def assign_operator(self, agency: str, block_id: str, operator_id: str, key: str, **kw: Any) -> Result[Block]:
        return self._call("assign_operator", agency, block_id, operator_id, key, **kw)

    def publish_rider_alert(self, agency: str, block_id: str, message: str, key: str) -> Result[dict]:
        return self._call("publish_rider_alert", agency, block_id, message, key)


class Registry:
    def __init__(self, config_dir: Path | str | None = None, *, clients: dict[str, httpx.Client] | None = None,
                 crosswalk: Crosswalk | None = None, idem: IdemStore | None = None,
                 killswitch: KillSwitchStore | None = None, sleep: Callable[[float], None] | None = None,
                 extra_adapters: dict[str, AdapterFactory] | None = None):
        self.config_dir = Path(config_dir) if config_dir else CONFIG_DIR
        self._clients = dict(clients or {})
        self.crosswalk = crosswalk or StaticCrosswalk()
        self.idem = idem or IdemStore()
        self.killswitch = killswitch or default_store()
        self._sleep = sleep
        self.adapters: dict[str, AdapterFactory] = {**BUILTIN_ADAPTERS, **(extra_adapters or {})}
        self._cache: dict[str, ToolSet] = {}

    def register_adapter(self, adapter_id: str, factory: AdapterFactory) -> None:
        self.adapters[adapter_id] = factory
        self._cache.clear()

    def _config(self, agency: str) -> dict:
        path = self.config_dir / f"{agency}.yaml"
        if not path.exists():
            raise KeyError(f"no tool registry config for agency {agency!r} ({path})")
        return yaml.safe_load(path.read_text())

    def _deps(self, cfg: dict) -> Deps:
        clients = dict(self._clients)
        for product, p in (cfg.get("products") or {}).items():
            if product not in clients and p.get("base_url"):
                clients[product] = httpx.Client(base_url=p["base_url"], timeout=TIMEOUT_S)
        kw: dict[str, Any] = {}
        if self._sleep:
            kw["sleep"] = self._sleep
        return Deps(clients=clients, crosswalk=self.crosswalk, idem=self.idem, killswitch=self.killswitch,
                    policy_for=lambda a: load_policy(a, self.config_dir), **kw)

    def toolset(self, agency: str) -> ToolSet:
        if agency in self._cache:
            return self._cache[agency]
        cfg = self._config(agency)
        deps = self._deps(cfg)
        instances: dict[str, Any] = {}
        impl: dict[str, Callable[..., Result]] = {}
        for tool, adapter_id in (cfg.get("tools") or {}).items():
            if adapter_id not in self.adapters:
                raise KeyError(f"agency {agency}: tool {tool} -> unknown adapter {adapter_id!r}")
            inst = instances.setdefault(adapter_id, self.adapters[adapter_id](deps))
            impl[tool] = getattr(inst, tool)
        ts = ToolSet(agency, impl, dict(cfg["tools"]), list(instances.values()))
        self._cache[agency] = ts
        return ts


__all__ = ["READ_TOOLS", "SAGA_ORDER", "WRITE_TOOLS", "Registry", "ToolSet"]
