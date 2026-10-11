"""Run all scenarios, print a table, write JSON.

    uv run python -m transitos.verification.report [--world pkg.module:factory] [--out report.json]

The default world is the FakeWorld (harness self-check). Pass the integration lead's factory
(a zero-arg callable returning a `World`) to verify the real system.
"""
from __future__ import annotations

import argparse
import importlib
import json
import sys
from collections.abc import Callable

from .latency import KEYS, LATENCY_NOTE
from .runner import ScenarioResult, run_scenario
from .scenarios import SCENARIOS, Scenario
from .world import World


def load_world_factory(spec: str | None) -> Callable[[], World]:
    if not spec or spec == "fake":
        from .fake_world import FakeWorld
        return FakeWorld
    mod, _, attr = spec.partition(":")
    return getattr(importlib.import_module(mod), attr)


def run_all(factory: Callable[[], World], scenarios: tuple[Scenario, ...] = SCENARIOS) -> list[ScenarioResult]:
    results = []
    for sc in scenarios:
        try:
            results.append(run_scenario(factory(), sc))
        except Exception as e:  # noqa: BLE001 - a crashing scenario is a failed scenario, not a crashed report
            results.append(ScenarioResult(scenario_id=sc.id, title=sc.title, expected=sc.expected.value,
                                          outcome="ERROR", mismatches=[f"{type(e).__name__}: {e}"]))
    return results


def format_table(results: list[ScenarioResult]) -> str:
    rows = [("scenario", "expected", "outcome", "invariants", "result")]
    for r in results:
        bad = [k for k, v in r.invariants.items() if v]
        verdict = ("SKIP" if r.skipped else "PASS" if r.passed else "FAIL")
        if r.known_gap and r.passed and not r.skipped:
            verdict = f"PASS (known gap: {r.known_gap})"
        rows.append((r.scenario_id, r.expected, r.outcome, "pass" if not bad else "FAIL " + ",".join(bad), verdict))
    widths = [max(len(row[i]) for row in rows) for i in range(5)]
    lines = ["  ".join(c.ljust(w) for c, w in zip(row, widths)) for row in rows]
    lines.insert(1, "  ".join("-" * w for w in widths))
    n_ok = sum(r.passed for r in results)
    n_skip = sum(bool(r.skipped) for r in results)
    lines.append(f"\n{n_ok}/{len(results)} scenarios passed" + (f" ({n_skip} skipped)" if n_skip else ""))
    for r in results:
        if r.skipped:
            lines.append(f"  [{r.scenario_id}] SKIP: {r.skipped}")
    for r in results:
        for m in r.mismatches:
            lines.append(f"  [{r.scenario_id}] {m}")
        for inv, vs in r.invariants.items():
            for v in vs:
                lines.append(f"  [{r.scenario_id}] {inv}: {v.detail}")
    return "\n".join(lines)


def format_latency(results: list[ScenarioResult]) -> str:
    """Detection latency per scenario that has a world engine (simulated seconds; '-' = not measurable)."""
    rows = [("scenario", *KEYS)]
    for r in results:
        if r.latency is not None:
            rows.append((r.scenario_id, *("-" if r.latency.get(k) is None else f"{r.latency[k]:.1f}" for k in KEYS)))
    if len(rows) == 1:
        return ""
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    lines = ["  ".join(c.ljust(w) for c, w in zip(row, widths)) for row in rows]
    lines.insert(1, "  ".join("-" * w for w in widths))
    return ("detection latency (simulated seconds from the physical fault)\n" + "\n".join(lines)
            + "\nNote: " + LATENCY_NOTE)


def to_json(results: list[ScenarioResult]) -> dict:
    return {
        "passed": sum(r.passed for r in results), "total": len(results),
        "scenarios": [{
            "id": r.scenario_id, "title": r.title, "expected": r.expected, "outcome": r.outcome,
            "mismatches": r.mismatches, "passed": r.passed,
            "invariants": {k: [v.model_dump() for v in vs] for k, vs in r.invariants.items()},
            "not_checked": list(r.not_checked),
            "latency": r.latency,    # sim seconds; null for scenarios without a world engine
            "latency_note": LATENCY_NOTE if r.latency is not None else None,
            "known_gap": r.known_gap, "skipped": r.skipped,
        } for r in results]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--world", default="fake")
    ap.add_argument("--out", default="verification-report.json")
    args = ap.parse_args(argv)
    results = run_all(load_world_factory(args.world))
    print(format_table(results))
    if lat := format_latency(results):
        print("\n" + lat)
    with open(args.out, "w") as f:
        json.dump(to_json(results), f, indent=2)
    print(f"\nwrote {args.out}")
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
