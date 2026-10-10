from __future__ import annotations

import pytest

from transitos.workflow.fakes import FakeClock, FakeTools, default_policy, default_world
from transitos.workflow.graph import Deps, build_graph, make_checkpointer, run_config
from transitos.workflow.limits import HardLimits
from transitos.workflow.store import RunsIndex


class Env:
    def __init__(self, tmp_path, **kw):
        self.db = str(tmp_path / "ckpt.sqlite")
        self.world = kw.pop("world", None) or default_world()
        self.tools = FakeTools(self.world)
        self.policy = kw.pop("policy", None) or default_policy()
        self.clock = FakeClock()
        self.store = RunsIndex(str(tmp_path / "runs.sqlite"))
        self.deps_kw = kw
        self.rebuild()

    def rebuild(self):
        """Fresh graph + checkpointer connection on the SAME sqlite file (simulates process restart)."""
        self.deps = Deps(tools=self.tools, policy=lambda: self.policy, limits=HardLimits(self.policy.limits),
                         store=self.store, clock=self.clock, **self.deps_kw)
        self.graph = build_graph(self.deps, make_checkpointer(self.db))
        return self.graph

    def state(self, run_id):
        return self.graph.get_state(run_config(run_id))


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


@pytest.fixture
def make_env(tmp_path):
    return lambda **kw: Env(tmp_path, **kw)
