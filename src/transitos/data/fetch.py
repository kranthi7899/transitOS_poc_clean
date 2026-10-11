"""P1: polite fetch of the manifest's external datasets (the ONLY network step; `make data`).

Reads the hand-curated data/MANIFEST.yaml (never written) and writes results to data/MANIFEST.lock.json plus a
PROVENANCE.json beside the downloaded files. Rules: manifest user agent, throttle between requests, timeouts, no
retries, failures are recorded not raised, `reference_only` is never requested, and a link on a host that is not
already in the manifest is reported for owner approval, never followed.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal
from urllib.parse import unquote, urljoin, urlparse

import httpx
import yaml
from pydantic import BaseModel, ConfigDict

MAX_BYTES = 150 * 1024 * 1024
LINK_RE = re.compile(r"""href\s*=\s*["']([^"']+\.(?:xlsx|xls|csv|zip))(?:\?[^"']*)?["']""", re.I)


class Dataset(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    kind: str
    fetch: Literal["auto", "manual", "reference_only"]
    url: str
    landing: str | None = None
    format: str | None = None
    license: str | None = None
    expected_rows: int | None = None
    target_path: str
    used_for: str | None = None


class Manifest(BaseModel):
    version: int
    user_agent: str
    throttle_seconds: float = 1.0
    datasets: list[Dataset]

    def hosts(self) -> set[str]:
        out = set()
        for d in self.datasets:
            for u in (d.url, d.landing):
                if u:
                    h = urlparse(u).hostname
                    if h:
                        out.add(h)
        return out


def load_manifest(path: Path, extra: Path | None = None) -> Manifest:
    """Load the hand-curated manifest; optionally merge builder-owned extras (same hosts only, unique ids)."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"manifest not found: {path}")
    m = Manifest.model_validate(yaml.safe_load(path.read_text()))
    if extra is not None and Path(extra).exists():
        hosts = m.hosts()
        ids = {d.id for d in m.datasets}
        for raw in yaml.safe_load(Path(extra).read_text()).get("datasets", []):
            d = Dataset.model_validate(raw)
            if d.id in ids:
                raise ValueError(f"duplicate dataset id in extra sources: {d.id}")
            host = urlparse(d.url).hostname
            if host not in hosts:
                raise ValueError(f"extra source {d.id} uses host {host}, not in the manifest (ask the owner)")
            ids.add(d.id)
            m.datasets.append(d)
    return m


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Fetcher:
    def __init__(self, manifest: Manifest, root: Path, client: httpx.Client | None = None,
                 now: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.m = manifest
        self.root = Path(root)
        self.client = client or httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0), follow_redirects=True)
        self._now, self._sleep = now, sleep
        self.request_times: list[float] = []
        self.lock_path = self.root / "data" / "MANIFEST.lock.json"
        self.lock: dict = {"datasets": {}}
        if self.lock_path.exists():
            self.lock = json.loads(self.lock_path.read_text())

    # -- http ------------------------------------------------------------------------------------------------
    def _get(self, url: str) -> httpx.Response:
        if self.request_times:
            wait = self.m.throttle_seconds - (self._now() - self.request_times[-1])
            if wait > 0:
                self._sleep(wait)
        self.request_times.append(self._now())
        resp = self.client.get(url, headers={"User-Agent": self.m.user_agent})
        if len(resp.content) > MAX_BYTES:
            raise ValueError(f"response larger than {MAX_BYTES} bytes")
        return resp

    # -- storage ---------------------------------------------------------------------------------------------
    def _save(self, ds: Dataset, dest: Path, resp: httpx.Response, url: str, source_page: str | None) -> dict:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resp.content)
        rec = {
            "url": url, "fetched_at": _utc_now(), "http_status": resp.status_code, "bytes": len(resp.content),
            "sha256": hashlib.sha256(resp.content).hexdigest(), "licence": ds.license,
            "source_page": source_page, "content_type": resp.headers.get("content-type"),
        }
        pj = dest.parent / "PROVENANCE.json"
        prov = json.loads(pj.read_text()) if pj.exists() else {"files": {}}
        prov["files"][dest.name] = rec
        pj.write_text(json.dumps(prov, indent=2, sort_keys=True) + "\n")
        return rec

    def _prior_sha(self, ds_id: str) -> str | None:
        return self.lock["datasets"].get(ds_id, {}).get("sha256")

    def _write_lock(self) -> None:
        self.lock["generated_at"] = _utc_now()
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path.write_text(json.dumps(self.lock, indent=2, sort_keys=True) + "\n")

    # -- per-kind --------------------------------------------------------------------------------------------
    def _auto(self, ds: Dataset) -> dict:
        resp = self._get(ds.url)
        out = {"status": "fetched" if resp.status_code == 200 else "failed", "http_status": resp.status_code,
               "content_type": resp.headers.get("content-type"), "verified": False, "url": ds.url}
        if resp.status_code != 200:
            out["error"] = f"HTTP {resp.status_code}"
            return out
        dest = self.root / ds.target_path
        rec = self._save(ds, dest, resp, ds.url, ds.landing)
        out.update(bytes=rec["bytes"], sha256=rec["sha256"], fetched_at=rec["fetched_at"], path=ds.target_path)
        prior = self._prior_sha(ds.id)
        if prior and prior != rec["sha256"]:
            out["sha256_changed"] = True
        return out

    def _manual(self, ds: Dataset) -> dict:
        resp = self._get(ds.url)
        ctype = resp.headers.get("content-type", "")
        out = {"status": "manual_probed" if resp.status_code == 200 else "failed", "http_status": resp.status_code,
               "content_type": ctype, "verified": False, "url": ds.url}
        if resp.status_code != 200:
            out["error"] = f"HTTP {resp.status_code}"
            return out
        target = self.root / ds.target_path
        if "html" in ctype.lower():
            hosts = self.m.hosts()
            links, new_hosts = [], []
            for href in dict.fromkeys(LINK_RE.findall(resp.text)):
                full = urljoin(ds.url, href)
                host = urlparse(full).hostname
                ok = host in hosts
                links.append({"url": full, "allowed_host": ok})
                if not ok and host not in new_hosts:
                    new_hosts.append(host)
            out["candidate_links"] = links
            out["new_hosts_needing_approval"] = new_hosts
            downloaded = []
            for link in links:
                if not link["allowed_host"]:
                    continue
                name = unquote(Path(urlparse(link["url"]).path).name)
                r = self._get(link["url"])
                if r.status_code == 200:
                    rec = self._save(ds, target / name, r, link["url"], ds.url)
                    downloaded.append({"file": name, "sha256": rec["sha256"], "bytes": rec["bytes"],
                                       "content_type": rec["content_type"]})
                else:
                    link["error"] = f"HTTP {r.status_code}"
            out["downloaded"] = downloaded
        else:
            # a direct probe (e.g. a Socrata CSV with a small $limit): keep the response itself
            name = "probe" + (".csv" if "csv" in ctype.lower() else ".txt")
            rec = self._save(ds, target / name, resp, ds.url, ds.landing)
            out.update(bytes=rec["bytes"], sha256=rec["sha256"], fetched_at=rec["fetched_at"],
                       path=f"{ds.target_path}{name}")
        return out

    # -- driver ----------------------------------------------------------------------------------------------
    def run(self, only: list[str] | None = None) -> dict:
        for ds in self.m.datasets:
            if only and ds.id not in only:
                continue
            if ds.fetch == "reference_only":
                entry = {"status": "reference_only", "verified": False, "url": ds.url}
            else:
                try:
                    entry = self._auto(ds) if ds.fetch == "auto" else self._manual(ds)
                except Exception as e:  # recorded, never raised
                    entry = {"status": "failed", "verified": False, "url": ds.url,
                             "error": f"{type(e).__name__}: {e}"}
            self.lock["datasets"][ds.id] = entry
            self._write_lock()
        self._write_lock()
        return self.lock


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    root = Path.cwd()
    only = argv or None
    manifest = load_manifest(root / "data" / "MANIFEST.yaml", extra=root / "data" / "EXTRA_SOURCES.yaml")
    lock = Fetcher(manifest, root).run(only=only)
    for k, v in lock["datasets"].items():
        print(f"{k}: {v['status']} http={v.get('http_status')} bytes={v.get('bytes')} {v.get('error', '')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
