"""Fetch stage tests: stubbed HTTP transport only, no network."""
from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from transitos.data.fetch import Fetcher, load_manifest

MANIFEST = """
version: 1
user_agent: "test-agent/1"
throttle_seconds: 1.0
datasets:
  - {id: a, kind: api, fetch: auto, url: "https://h1.example/a.csv", format: csv, license: "PD", landing: "https://h1.example/page", target_path: data/raw/a/a.csv}
  - {id: b, kind: api, fetch: auto, url: "https://h1.example/b.csv", format: csv, license: "PD", target_path: data/raw/a/b.csv}
  - {id: c, kind: repo, fetch: reference_only, url: "https://h2.example/c", format: git, license: null, target_path: data/raw/c/}
  - {id: d, kind: landing, fetch: auto, url: "https://h3.example/d.csv", format: csv, license: "PD", target_path: data/raw/d/d.csv}
  - {id: m, kind: landing, fetch: manual, url: "https://h1.example/landing", format: xlsx, license: "PD", target_path: data/raw/m/}
"""

LANDING = '<html><a href="/files/data%20set.xlsx">x</a><a href="https://other.example/z.xlsx">y</a></html>'


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


def build(tmp_path, handler):
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "MANIFEST.yaml").write_text(MANIFEST)
    clock = Clock()
    client = httpx.Client(transport=httpx.MockTransport(handler))
    m = load_manifest(tmp_path / "data" / "MANIFEST.yaml")
    return Fetcher(m, root=tmp_path, client=client, now=clock.now, sleep=clock.sleep)


def handler_ok(seen):
    def h(req: httpx.Request):
        seen.append(req)
        if req.url.host == "h3.example":
            raise httpx.ConnectError("boom")
        if req.url.path == "/landing":
            return httpx.Response(200, text=LANDING, headers={"content-type": "text/html"})
        if req.url.path.endswith(".xlsx"):
            return httpx.Response(200, content=b"XL", headers={"content-type": "application/vnd.ms-excel"})
        return httpx.Response(200, text="h\n1\n", headers={"content-type": "text/csv"})
    return h


def lock_of(tmp_path):
    return json.loads((tmp_path / "data/MANIFEST.lock.json").read_text())


def test_auto_download_writes_file_provenance_and_lock(tmp_path):
    seen = []
    build(tmp_path, handler_ok(seen)).run()
    assert (tmp_path / "data/raw/a/a.csv").read_text() == "h\n1\n"
    prov = json.loads((tmp_path / "data/raw/a/PROVENANCE.json").read_text())
    rec = prov["files"]["a.csv"]
    assert rec["sha256"] == hashlib.sha256(b"h\n1\n").hexdigest()
    assert rec["http_status"] == 200 and rec["bytes"] == 4 and rec["licence"] == "PD"
    assert rec["url"] == "https://h1.example/a.csv" and rec["source_page"] == "https://h1.example/page"
    assert "b.csv" in prov["files"]  # two datasets in one directory
    assert lock_of(tmp_path)["datasets"]["a"]["sha256"] == rec["sha256"]
    assert seen[0].headers["user-agent"] == "test-agent/1"


def test_throttle_between_requests(tmp_path):
    f = build(tmp_path, handler_ok([]))
    f.run()
    assert len(f.request_times) >= 3
    gaps = [b - a for a, b in zip(f.request_times, f.request_times[1:])]
    assert all(g >= 1.0 - 1e-9 for g in gaps)


def test_reference_only_never_fetched(tmp_path):
    seen = []
    build(tmp_path, handler_ok(seen)).run()
    assert all(r.url.host != "h2.example" for r in seen)
    assert lock_of(tmp_path)["datasets"]["c"]["status"] == "reference_only"
    assert not (tmp_path / "data/raw/c").exists()


def test_failing_link_recorded_not_raised(tmp_path):
    build(tmp_path, handler_ok([])).run()
    d = lock_of(tmp_path)["datasets"]["d"]
    assert d["verified"] is False and "boom" in d["error"]
    assert not (tmp_path / "data/raw/d/d.csv").exists()


def test_http_error_status_recorded_and_no_file(tmp_path):
    def h(req):
        return httpx.Response(404, text="nope", headers={"content-type": "text/html"})
    build(tmp_path, h).run()
    a = lock_of(tmp_path)["datasets"]["a"]
    assert a["http_status"] == 404 and a["verified"] is False
    assert not (tmp_path / "data/raw/a/a.csv").exists()


def test_manual_finds_link_records_it_and_flags_new_host(tmp_path):
    seen = []
    build(tmp_path, handler_ok(seen)).run()
    m = lock_of(tmp_path)["datasets"]["m"]
    links = {x["url"]: x for x in m["candidate_links"]}
    assert links["https://h1.example/files/data%20set.xlsx"]["allowed_host"] is True
    assert links["https://other.example/z.xlsx"]["allowed_host"] is False
    assert m["new_hosts_needing_approval"] == ["other.example"]
    assert (tmp_path / "data/raw/m/data set.xlsx").read_bytes() == b"XL"
    assert all(r.url.host != "other.example" for r in seen)


def test_second_run_changed_sha_flagged(tmp_path):
    state = {"body": "one"}

    def h(req):
        return httpx.Response(200, text=state["body"], headers={"content-type": "text/csv"})
    build(tmp_path, h).run(only=["a"])
    state["body"] = "two"
    build(tmp_path, h).run(only=["a"])
    assert lock_of(tmp_path)["datasets"]["a"]["sha256_changed"] is True


def test_manifest_file_not_modified(tmp_path):
    f = build(tmp_path, handler_ok([]))
    before = (tmp_path / "data/MANIFEST.yaml").read_text()
    f.run()
    assert (tmp_path / "data/MANIFEST.yaml").read_text() == before


def test_missing_manifest_fails_loudly(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_manifest(tmp_path / "nope.yaml")


def test_extra_sources_merged_and_host_checked(tmp_path):
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "MANIFEST.yaml").write_text(MANIFEST)
    good = "version: 1\ndatasets:\n  - {id: x, kind: api, fetch: auto, url: 'https://h1.example/x.xml', target_path: data/raw/x/x.xml}\n"
    (tmp_path / "data" / "EXTRA_SOURCES.yaml").write_text(good)
    m = load_manifest(tmp_path / "data" / "MANIFEST.yaml", extra=tmp_path / "data" / "EXTRA_SOURCES.yaml")
    assert [d.id for d in m.datasets][-1] == "x"
    bad = good.replace("h1.example", "evil.example")
    (tmp_path / "data" / "EXTRA_SOURCES.yaml").write_text(bad)
    with pytest.raises(ValueError, match="evil.example"):
        load_manifest(tmp_path / "data" / "MANIFEST.yaml", extra=tmp_path / "data" / "EXTRA_SOURCES.yaml")
    dup = good.replace("id: x", "id: a")
    (tmp_path / "data" / "EXTRA_SOURCES.yaml").write_text(dup)
    with pytest.raises(ValueError, match="duplicate"):
        load_manifest(tmp_path / "data" / "MANIFEST.yaml", extra=tmp_path / "data" / "EXTRA_SOURCES.yaml")
