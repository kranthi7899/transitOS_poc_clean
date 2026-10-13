import importlib.util
import json
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("build_explorer", ROOT / "scripts" / "build_explorer.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key in ("src", "href") and value:
                self.links.append(value)


def test_replay_has_real_effects_and_exact_branch_endpoints():
    replay = builder.record_replay()
    states = replay["states"]
    assert states["proposal"]["writes"] == []
    assert states["rejected"]["writes"] == []
    assert len(states["done"]["writes"]) == 6
    assert states["done"]["runs"][0]["status"] == "DONE"
    assert states["done_direct"]["runs"][0]["status"] == "DONE"
    assert states["return"]["clock"] != states["return_direct"]["clock"]
    assert all(not state["invariant_violations"] and not state["memory"] for state in states.values())
    assert states["done"]["history_revisions"] == 10
    assert states["approved"]["product_records"]["scheduling"]["records"]["/blocks"][0]["vehicle"] == "BUS-45"
    assert states["done"]["notifications"]
    assert states["ready"]["product_baseline"] == states["done"]["product_baseline"]
    for edges in replay["edges"].values():
        assert all(target in states for target in edges.values())


def test_site_uses_relative_assets_and_an_allowlist(tmp_path):
    guide = builder.build(tmp_path)
    assert len(guide["modules"]) == len(builder.SOURCES)
    assert {m["path"] for m in guide["modules"]} == {s[2] for s in builder.SOURCES}
    for html in tmp_path.rglob("*.html"):
        parser = Links()
        parser.feed(html.read_text())
        for link in parser.links:
            assert not link.startswith("/"), (html, link)  # GitHub project subpaths must work
            if not link.startswith(("http", "#")):
                assert (html.parent / link.split("#")[0]).exists(), (html, link)
    assert not list(tmp_path.rglob("*.sqlite")) and not list(tmp_path.rglob(".env*"))
    replay = json.loads((tmp_path / "demo" / "replay.json").read_text())
    assert replay["mode"] == "recorded_replay" and all(s["spend"] is None for s in replay["states"].values())


def test_source_links_point_to_existing_symbols_and_content_hashes():
    import hashlib
    for module in builder.source_guide():
        raw = (ROOT / module["path"]).read_text()
        assert module["sha256"] == hashlib.sha256(raw.encode()).hexdigest()
        assert 1 <= module["line"] <= len(raw.splitlines())
        assert f"#L{module['line']}" in module["url"]
