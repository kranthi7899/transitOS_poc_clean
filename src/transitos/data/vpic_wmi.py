"""One-time, cached check of WMI candidates against NHTSA vPIC (Phase 10 Step 2, task 2.1). NOT run by tests.

    uv run python -m transitos.data.vpic_wmi            # uses the cache; calls vPIC only for uncached requests

Budget: at most 12 calls in total, at least 1 second apart, user agent below; every response is cached under
data/raw/vpic/cache/ (gitignored). Synthetic candidates are checked with DecodeVinValues on ONE probe VIN each
(built with transitos.data.identifiers.make_vin before the WMI is adopted); the published real WMIs with
GetWMIsForManufacturer. Writes fixtures/synthetic_wmis.yaml (only the WMIs vPIC does not recognise) and
fixtures/realistic_wmis.yaml (opt-in; only the real WMIs vPIC lists for that manufacturer).
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from urllib.parse import quote

import yaml

from .identifiers import make_vin

ROOT = Path(__file__).resolve().parents[3]
CACHE = ROOT / "data" / "raw" / "vpic" / "cache"
HOST = "https://vpic.nhtsa.dot.gov/api/vehicles"
UA = "transitos-poc/0.1 (research; non-commercial; contact via repository owner)"
MAX_CALLS = 12

SYNTHETIC_CANDIDATES = ["0ZB", "0ZT", "0ZX", "0TZ", "ZZ7"]
# REPORTED real WMIs (research digest section 3), checked against the manufacturer's vPIC list
REAL = [("1FD", "Ford"), ("1BA", "Blue Bird"), ("1BD", "Blue Bird"), ("15G", "Gillig"),
        ("5FY", "New Flyer"), ("1N9", "ElDorado")]
PROBE = {"vds": "B1C2D", "model_year": 2024, "plant": "Z", "serial": 1}


class Budget:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self._last = 0.0

    def get(self, path: str) -> dict:
        key = CACHE / (path.replace("/", "_").replace("?", "_").replace("=", "_").replace("&", "_") + ".json")
        if key.exists():
            return json.loads(key.read_text())
        if len(self.calls) >= MAX_CALLS:
            raise RuntimeError(f"vPIC call budget ({MAX_CALLS}) exhausted")
        import httpx
        wait = 1.05 - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        url = f"{HOST}/{path}"
        r = httpx.get(url, headers={"User-Agent": UA}, timeout=30)
        self._last = time.monotonic()
        self.calls.append(f"{r.status_code} {url}")
        r.raise_for_status()
        CACHE.mkdir(parents=True, exist_ok=True)
        key.write_text(r.text)
        return r.json()


def probe_vin(wmi: str) -> str:
    return make_vin(wmi, **PROBE)


def recognised(decoded: dict) -> bool:
    """A probe VIN's WMI counts as recognised when vPIC names a make or manufacturer for it."""
    row = (decoded.get("Results") or [{}])[0]
    return bool((row.get("Make") or "").strip() or (row.get("Manufacturer") or "").strip()
                or (row.get("ManufacturerId") or "").strip())


def main() -> int:
    b = Budget()
    synth, synth_log = [], []
    for wmi in SYNTHETIC_CANDIDATES:
        vin = probe_vin(wmi)
        d = b.get(f"DecodeVinValues/{vin}?format=json")
        row = (d.get("Results") or [{}])[0]
        synth_log.append({"wmi": wmi, "probe_vin": vin, "make": row.get("Make", ""),
                          "manufacturer": row.get("Manufacturer", ""), "error_code": row.get("ErrorCode", "")})
        if not recognised(d):
            synth.append(wmi)
    real, real_log = [], []
    by_maker: dict[str, list[dict]] = {}
    for wmi, maker in REAL:
        if maker not in by_maker:
            by_maker[maker] = b.get(f"GetWMIsForManufacturer/{quote(maker)}?format=json").get("Results") or []
        # a low-volume maker's identifier is six characters (565.15(a): '9' in position 3, continued in 12-14)
        hits = [r for r in by_maker[maker] if (w := (r.get("WMI") or "").upper()) == wmi
                or (len(w) == 6 and w[:3] == wmi and wmi[2] == "9")]
        real_log.append({"wmi": wmi, "searched": maker, "listed": bool(hits),
                         "listed_as": hits[0].get("WMI", "") if hits else "",
                         "name": hits[0].get("Name", "") if hits else "",
                         "vehicle_type": hits[0].get("VehicleType", "") if hits else ""})
        if hits:
            real.append({"wmi": hits[0]["WMI"].upper(), "manufacturer": hits[0].get("Name", maker),
                         "vehicle_type": hits[0].get("VehicleType", "")})
    stamp = time.strftime("%Y-%m-%d")
    (ROOT / "fixtures" / "synthetic_wmis.yaml").write_text(
        "# WMIs that NHTSA vPIC did NOT recognise when checked once (DecodeVinValues on one probe VIN each).\n"
        f"# Checked {stamp} by transitos.data.vpic_wmi; responses cached under data/raw/vpic/cache/ (gitignored).\n"
        "# Synthetic VINs built on these are fictitious: never look them up in any service (D-F, A77).\n"
        + yaml.safe_dump({"synthetic": True, "source": "vPIC DecodeVinValues", "checked_on": stamp,
                          "wmis": synth, "checked": synth_log}, sort_keys=False))
    (ROOT / "fixtures" / "realistic_wmis.yaml").write_text(
        "# OPT-IN realistic-decode fixture: real WMIs, kept ONLY where vPIC GetWMIsForManufacturer lists them.\n"
        f"# Checked {stamp} by transitos.data.vpic_wmi. Not used by the default fleets (D-F, A77).\n"
        + yaml.safe_dump({"opt_in": True, "source": "vPIC GetWMIsForManufacturer", "checked_on": stamp,
                          "wmis": real, "checked": real_log}, sort_keys=False))
    print(json.dumps({"calls": b.calls, "synthetic": synth, "real": [r["wmi"] for r in real]}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
