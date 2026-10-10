"""World interface + factory for the Level 2 acceptance tests.

The Protocol lives in `transitos.verification.world` (so the report CLI can use it without importing
tests); it is re-exported here.  The integration lead supplies the real World by either

  * setting env var TRANSITOS_WORLD="package.module:factory"  (zero-arg callable returning a World), or
  * providing `transitos.integration.world:make_world`.

Until one of those exists the "real" parametrisation of every e2e test is SKIPPED with a clear reason;
the "fake" parametrisation (FakeWorld) keeps running so the invariants and scenarios stay proven.
"""
from __future__ import annotations

import importlib
import os
from collections.abc import Callable

from transitos.verification.fake_world import FakeWorld
from transitos.verification.world import World

__all__ = ["NO_REAL_WORLD_REASON", "FakeWorld", "World", "real_world_factory"]

DEFAULT_SPEC = "transitos.integration.world:make_world"
NO_REAL_WORLD_REASON = (
    "real World not provided yet: set TRANSITOS_WORLD=module:factory or add "
    f"{DEFAULT_SPEC} (integration lead). Running against FakeWorld only.")


def real_world_factory() -> Callable[[], World] | None:
    spec = os.environ.get("TRANSITOS_WORLD", DEFAULT_SPEC)
    mod, _, attr = spec.partition(":")
    try:
        return getattr(importlib.import_module(mod), attr)
    except (ImportError, AttributeError):
        return None
