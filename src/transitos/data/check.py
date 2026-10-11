"""`make data-check`: OFFLINE. Reads only committed files and validates each through its loader.

Exit 0 when every required file exists and validates, else exit 1 with one line per problem.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

from transitos.data import loaders


@dataclass
class Report:
    checked: list[tuple[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def check_all(root: Path | None = None) -> Report:
    root = Path(root) if root else loaders.DEFAULT_ROOT
    rep = Report()
    loaded: dict = {}

    def run(name: str, fn, optional: bool = False):
        try:
            obj = fn(root)
        except Exception as e:  # noqa: BLE001  report every problem, not just the first
            rep.errors.append(f"{name}: {type(e).__name__}: {e}")
            return
        if obj is None and optional:
            rep.checked.append((name, "absent (optional)"))
            return
        loaded[name] = obj
        rep.checked.append((name, "ok"))

    run("fault_rates", loaders.load_fault_rates)
    run("fleet_mix", loaders.load_fleet_mix)
    run("repair_durations", loaders.load_repair_durations, optional=True)
    run("j1939_spn_fmi", loaders.load_j1939)
    run("fault_mix", loaders.load_fault_mix)
    run("shop_behaviour", loaders.load_shop_behaviour)

    # cross-file consistency
    j, mix = loaded.get("j1939_spn_fmi"), loaded.get("fault_mix")
    if j and mix:
        known = {r.spn for r in j.spn}
        for sysname, s in mix.systems.items():
            missing = set(s.primary_spns) - known
            if missing:
                rep.errors.append(f"fault_mix: system {sysname} references SPNs not in the fixture: {sorted(missing)}")
        for r in j.spn:
            if r.system not in mix.systems:
                rep.errors.append(f"j1939_spn_fmi: SPN {r.spn} system {r.system!r} not in fault_mix systems")
    return rep


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    rep = check_all(Path(argv[0]) if argv else None)
    for name, status in rep.checked:
        print(f"data-check: {name}: {status}")
    for e in rep.errors:
        print(f"data-check: ERROR {e}")
    print("data-check: OK" if rep.ok else "data-check: FAILED")
    return 0 if rep.ok else 1


if __name__ == "__main__":
    sys.exit(main())
