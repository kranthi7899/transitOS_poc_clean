import pytest
from world import NO_REAL_WORLD_REASON, FakeWorld, real_world_factory


@pytest.fixture(params=["fake", "real"])
def make_world(request):
    """Zero-arg factory returning a fresh World. 'real' is skipped until the integration lead provides it."""
    if request.param == "fake":
        return FakeWorld
    factory = real_world_factory()
    if factory is None:
        pytest.skip(NO_REAL_WORLD_REASON)
    return factory
