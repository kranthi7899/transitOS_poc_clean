"""Clearance: the one place hard rules, graded fit and missing-data semantics live (Phase 10 amendment A).

Pure functions over plain facts. Called by the planner (workflow), the write adapters, `validate_output`
and verification. Assumption ids A65-A74 live in docs/assumptions/phase10-step1.md."""
from .evaluate import Assign, Clearance, Option, evaluate_option, safe_evaluate
from .facts import CapabilityProfile, Datum, Facts
from .rank import RankedList, rank_options

__all__ = ["Assign", "CapabilityProfile", "Clearance", "Datum", "Facts", "Option", "RankedList",
           "evaluate_option", "rank_options", "safe_evaluate"]
