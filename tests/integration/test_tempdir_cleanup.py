"""Hygiene: the integrated system must not leak a temp directory per instance.

Found in Phase 10: ~18,000 `transitos-*` directories (~13 GB) piled up from test runs and filled the disk."""
import gc
from pathlib import Path

from transitos.integration.system import System
from transitos.integration.world import IntegrationWorld
from transitos.verification.scenarios import BY_ID

from .conftest import AG, Mocks


def test_a_system_that_made_its_own_directory_removes_it_when_collected():
    m = Mocks()
    s = System(AG, m)                      # no data_dir: the system makes (and owns) one
    path = Path(s.dir)
    assert path.exists()
    s.close()
    del s
    gc.collect()
    assert not path.exists()


def test_a_directory_the_caller_supplied_is_never_removed(tmp_path):
    m = Mocks()
    s = System(AG, m, tmp_path / "mine")
    s.close()
    del s
    gc.collect()
    assert (tmp_path / "mine").exists()


def test_the_integration_world_removes_its_previous_directory_on_the_next_scenario():
    w = IntegrationWorld()
    w.apply(BY_ID["severe_fault_with_spare"])
    first = Path(w.system.dir)
    assert first.exists()
    w.apply(BY_ID["last_spare"])
    second = Path(w.system.dir)
    assert second != first and second.exists()
    assert not first.exists()
