"""Step 3.3 stochastic mode: seeded, byte-identical for a fixed seed, rates from the REAL NTD-derived quantiles
(bus, pooled 2022-24, p10 5,699 / p50 18,576 / p90 112,305 all agencies; small-fleet cut p50 26,657), everything else
ASSUMED. Expected figures below are written from the spec text, not read back from the implementation."""
import ast
import pathlib
from datetime import UTC, datetime

import pytest

from transitos.data.loaders import load_fault_mix, load_j1939
from transitos.world.script import DayScript
from transitos.world.stochastic import (
    CALLOUT_P,
    CALLOUT_P_SENSITIVITY,
    StochasticConfig,
    generate_world,
)

START = datetime(2026, 10, 10, tzinfo=UTC)
VEHICLES = tuple(f"veh-{n}" for n in range(1, 13))
OPERATORS = tuple(f"op-{n}" for n in range(1, 11))


def cfg(**kw):
    base = {"seed": 11, "agency": "agency-042", "vehicles": VEHICLES, "operators": OPERATORS, "start": START,
            "days": 60, "revenue_miles_per_day": 150.0}
    base.update(kw)
    return StochasticConfig(**base)


def test_fixed_seed_reproduces_byte_for_byte_and_other_seeds_differ():
    a, b = generate_world(cfg()), generate_world(cfg())
    assert a.to_json() == b.to_json() and a.script.to_json() == b.script.to_json()
    assert len(a.script.beats) > 20
    assert generate_world(cfg(seed=12)).to_json() != a.to_json()


def test_input_order_does_not_change_the_timeline():
    a = generate_world(cfg())
    b = generate_world(cfg(vehicles=tuple(reversed(VEHICLES)), operators=tuple(reversed(OPERATORS))))
    assert a.to_json() == b.to_json()


def test_defaults_are_the_small_fleet_bus_p50_and_say_where_the_number_came_from():
    w = generate_world(cfg())
    m = w.meta
    assert (m["mode"], m["quantile"], m["fleet_cut"]) == ("MB", "p50", "small_fleet_voms_10_50")
    assert m["miles_between_major_failures"] == 26657           # data/derived/fault_rates.yaml, pooled 2022-24
    assert "NTD" in m["rate_source"] and "ASSUMED" in m["other_numbers"]


def test_quantile_and_cut_are_explicit_parameters():
    assert generate_world(cfg(quantile="p10", fleet_cut="all")).meta["miles_between_major_failures"] == 5699
    assert generate_world(cfg(quantile="p90", fleet_cut="all")).meta["miles_between_major_failures"] == 112305
    assert generate_world(cfg(quantile="p50", fleet_cut="all")).meta["miles_between_major_failures"] == 18576
    with pytest.raises(ValueError):
        generate_world(cfg(quantile="p99"))


def test_failure_count_matches_poisson_rate_in_revenue_miles():
    # 200 vehicles x 365 days x 150 mi = 10.95 M mi; p50 small-fleet 26,657 mi -> expected 410.8 failures, sd ~ 20.3
    big = cfg(vehicles=tuple(f"veh-{n}" for n in range(1, 201)), days=365, operators=())
    w = generate_world(big)
    n = w.meta["failures"]
    assert abs(n - 10_950_000 / 26_657) < 4 * 20.3
    # a lower quantile means more frequent failures, in proportion
    n10 = generate_world(cfg(vehicles=big.vehicles, days=365, operators=(), quantile="p10", fleet_cut="all")).meta["failures"]
    assert abs(n10 - 10_950_000 / 5_699) < 4 * 44


def test_unknown_mileage_is_an_error_never_zero_miles():
    with pytest.raises(ValueError):
        generate_world(cfg(revenue_miles_per_day={"veh-1": 150.0}))               # the other 11 vehicles are unknown
    with pytest.raises(ValueError):
        generate_world(cfg(revenue_miles_per_day=None))
    with pytest.raises(ValueError):
        generate_world(cfg(revenue_miles_per_day=-5.0))
    with pytest.raises(ValueError):
        generate_world(cfg(revenue_miles_per_day={**{v: 150.0 for v in VEHICLES}, "veh-3": None}))
    # an explicit, known zero (the bus did not run) is allowed and produces no failures for that vehicle
    w = generate_world(cfg(revenue_miles_per_day={**{v: 150.0 for v in VEHICLES}, "veh-3": 0.0}, days=400))
    assert all(b.vehicle != "veh-3" for b in w.script.beats if b.kind != "callout")


def test_systems_and_spns_come_from_the_committed_files():
    mix, j = load_fault_mix(), load_j1939()
    spn_system = {r.spn: r.system for r in j.spn}
    w = generate_world(cfg(days=400))
    faults = [b for b in w.script.beats if b.kind == "fault_on"]
    dvirs = [b for b in w.script.beats if b.kind == "dvir"]
    assert faults and dvirs
    for b in faults:
        assert spn_system[b.params["spn"]] == b.params["system"] and b.params["system"] in mix.systems
        assert b.params["fmi"] in j.fmi
    for b in dvirs:                                    # systems with no SPN in the fixture are reported by the driver
        assert b.params["system"] in mix.systems and not any(r.system == b.params["system"] for r in j.spn)
    assert {b.params["system"] for b in faults + dvirs} <= set(mix.systems)


def test_intermittent_faults_clear_and_recur():
    w = generate_world(cfg(days=900))
    by = {}
    for b in w.script.beats:
        if b.kind in ("fault_on", "fault_off"):
            by.setdefault((b.vehicle, b.params["fault_id"]), []).append(b.kind)
    recurring = [k for k in by.values() if k.count("fault_on") >= 2]
    assert recurring
    for seq in recurring:
        assert seq[0] == "fault_on" and all(seq[i] != seq[i + 1] for i in range(len(seq) - 1))   # strictly on/off/on/off...


def test_callouts_are_007_per_operator_shift_with_a_documented_sensitivity():
    assert (CALLOUT_P, CALLOUT_P_SENSITIVITY) == (0.07, 0.10)
    w = generate_world(cfg(days=3000))
    shifts = (len(OPERATORS) - len(w.extra_board)) * 3000        # the extra board covers; it is not scheduled to call out
    assert w.meta["operator_shifts"] == shifts
    n = sum(b.kind == "callout" for b in w.script.beats)
    assert abs(n - 0.07 * shifts) < 4 * (shifts * 0.07 * 0.93) ** 0.5
    w10 = generate_world(cfg(days=3000, callout_p=CALLOUT_P_SENSITIVITY))
    shifts10 = (len(OPERATORS) - len(w10.extra_board)) * 3000
    n10 = sum(b.kind == "callout" for b in w10.script.beats)
    assert abs(n10 - 0.10 * shifts10) < 4 * (shifts10 * 0.10 * 0.90) ** 0.5
    with pytest.raises(ValueError):
        generate_world(cfg(callout_p=1.5))


def test_extra_board_is_one_to_two_of_ten_operators():
    sizes = {len(generate_world(cfg(seed=s)).extra_board) for s in range(40)}
    assert sizes == {1, 2}
    w = generate_world(cfg())
    assert set(w.extra_board) <= set(OPERATORS) and list(w.extra_board) == sorted(w.extra_board)
    assert not any(b.params["operator"] in w.extra_board for b in w.script.beats if b.kind == "callout")


def test_scripts_roundtrip_and_beats_are_sorted():
    w = generate_world(cfg())
    assert DayScript.from_json(w.script.to_json()) == w.script
    ats = [b.at for b in w.script.beats]
    assert ats == sorted(ats)


BANNED_ATTR = {("time", "time"), ("datetime", "now"), ("datetime", "utcnow"), ("datetime", "today"), ("uuid", "uuid4"),
               ("os", "urandom"), ("random", "random"), ("random", "randint"), ("random", "choice"),
               ("random", "choices"), ("random", "shuffle"), ("random", "uniform"), ("random", "seed"),
               ("random", "sample"), ("random", "expovariate"),
               # Step 4 fix round F10: every module-level distribution draw (the unseeded global stream)
               *(("random", f) for f in ("lognormvariate", "gauss", "normalvariate", "betavariate", "gammavariate",
                                        "triangular", "vonmisesvariate", "weibullvariate", "paretovariate",
                                        "getrandbits", "randrange", "binomialvariate"))}
BANNED_IMPORT = {"requests", "httpx", "socket", "urllib", "secrets", "time"}


def determinism_violations(source: str) -> list[str]:
    """Wall clock, unseeded randomness and network use, found in the syntax tree (not by grep)."""
    out = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute):
            owner = node.value.attr if isinstance(node.value, ast.Attribute) else getattr(node.value, "id", None)
            if (owner, node.attr) in BANNED_ATTR:
                out.append(f"{owner}.{node.attr}")
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
            if name in ("Random", "SystemRandom") and not node.args and not node.keywords:
                out.append("unseeded Random()")
            if name == "SystemRandom":
                out.append("SystemRandom")
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names if a.name.split(".")[0] in BANNED_IMPORT]
        if isinstance(node, ast.ImportFrom):
            mod = (node.module or "").split(".")[0]
            if mod in BANNED_IMPORT:
                out.append(f"from {node.module}")
            if mod == "random":
                out += [f"from random import {a.name}" for a in node.names if a.name != "Random"]
            if mod == "datetime":
                out += [f"from datetime import {a.name}" for a in node.names if a.name in ("now", "utcnow")]
    return out


def test_the_determinism_guard_catches_what_it_claims_to():
    for bad in ("import datetime\ndatetime.datetime.now()", "from datetime import datetime\ndatetime.now()",
                "from random import random", "import random\nrandom.Random()", "import random\nrandom.random()",
                "import time", "from time import sleep", "import uuid\nuuid.uuid4()", "import httpx",
                "import random\nrandom.SystemRandom()"):
        assert determinism_violations(bad), bad
    for good in ("import random\nrandom.Random(3)", "from random import Random\nRandom('a|b')",
                 "from datetime import datetime\ndatetime.fromisoformat('2026-01-01T00:00:00+00:00')"):
        assert not determinism_violations(good), good


@pytest.mark.parametrize("fn", ["lognormvariate", "gauss", "normalvariate", "betavariate", "gammavariate",
                                "triangular", "vonmisesvariate", "weibullvariate", "paretovariate", "getrandbits",
                                "randrange", "binomialvariate"])
def test_the_guard_catches_module_level_distribution_draws(fn):
    """Step 4 fix round F10: the module-level `random.<distribution>` draws share the unseeded global stream."""
    assert determinism_violations(f"import random\nrandom.{fn}(1, 2)"), fn
    assert not determinism_violations(f"import random\nrng = random.Random('s')\nrng.{fn}(1, 2)"), fn


def test_world_package_and_its_hooks_have_no_wall_clock_unseeded_randomness_or_network():
    src = pathlib.Path(__file__).resolve().parents[2] / "src" / "transitos"
    files = [*sorted((src / "world").glob("*.py")), src / "verification" / "latency.py", src / "integration" / "world.py"]
    assert len(files) >= 10
    for f in files:
        assert not determinism_violations(f.read_text()), f.name
