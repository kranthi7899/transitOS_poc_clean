"""Phase 10 Step 3: the world engine (Track A). Ground truth + per-product lag + seeded stochastic mode.

Products here are SIMULATED. Every invented number is tagged A90-A104 (docs/assumptions/phase10-step3.md); the
failure rates are the exception: they are REAL NTD-derived quantiles (data/derived/fault_rates.yaml).
"""
from .engine import Observation, Skipped, WorldEngine
from .lag import ProductLag
from .script import Beat, DayScript
from .truth import GroundTruth

__all__ = ["Beat", "DayScript", "GroundTruth", "Observation", "ProductLag", "Skipped", "WorldEngine"]
