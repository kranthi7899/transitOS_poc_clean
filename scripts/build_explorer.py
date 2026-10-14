"""Build a static, no-key repository explorer and actual-system replay (A202).

No third-party code is copied. OpenWiki inspired the source/architecture/learning views.
Only the allowlisted sources and fresh synthetic recordings below can enter the site.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from transitos.integration.console import Command, DemoSession
from transitos.workflow.memory import IncidentMemory
from transitos.workflow.reasoning import StubReasoner

ROOT = Path(__file__).resolve().parents[1]
SOURCES = [
    ("products", "Simulated products", "src/transitos/mocks/telematics.py", "Signed fault webhooks start the story; five product mocks keep separate records.", "create_app"),
    ("events", "Events & identity", "src/transitos/plumbing/crosswalk_service.py", "Resolve product-native identifiers before a workflow can act. Unresolved evidence waits for a person.", "CrosswalkService"),
    ("system", "Integrated system", "src/transitos/integration/system.py", "Wires adapters, bus, crosswalk, workflow, tools and panel. The default needs no external infrastructure.", "System"),
    ("graph", "Durable workflow", "src/transitos/workflow/graph.py", "LangGraph checkpoints the response and interrupts for approval, repair and return. Interrupt nodes perform no writes.", "build_graph"),
    ("rules", "Code-enforced limits", "src/transitos/clearance/evaluate.py", "One pure clearance implementation checks options. Missing data is explicit; known violations exclude.", "safe_evaluate"),
    ("reasoner", "Proposal-only reasoning", "src/transitos/workflow/reasoning.py", "Validate model proposals against current feasible lists. Code-ranked fallback is visible and still needs approval.", "validate_output"),
    ("openai", "OpenAI adapter", "src/transitos/workflow/reasoning_openai.py", "Structured proposals with a persistent $0.50 app ceiling. No model tools or direct product writes.", "OpenAIReasoner"),
    ("tools", "Controlled actions", "src/transitos/tools/write_adapters.py", "Live kill-switch and safety rechecks, idempotency keys, product calls and read-back.", "SchedulingWriter"),
    ("console", "Dispatcher console", "src/transitos/integration/console.py", "One serialized worker drives the actual system. Graph updates, approvals and current product state reach the browser.", "DemoSession"),
    ("memory", "Scoped incident memory", "src/transitos/workflow/memory.py", "Completed, approved incident summaries survive resets. History never substitutes for fresh facts or authorization.", "IncidentMemory"),
    ("verification", "Verification & evals", "src/transitos/verification/invariants.py", "Check approval-before-write, hard limits, idempotency, audit completeness, alert order and the kill switch.", "check_all"),
]


def clean_snapshot(state):
    allowed = ("clock", "reasoner", "trace", "history", "assessment", "identities", "visible_alerts",
               "invariant_violations", "agency", "policy", "runs", "writes", "audit", "vehicles", "blocks",
               "operators", "work_orders", "alerts", "dead_letters", "events", "escalations",
               "product_records", "product_baseline", "notifications", "identity_bridge")
    out = {key: state[key] for key in allowed}
    out.update({"generation": "recorded-replay", "busy": False, "error": None, "spend": None, "memory": [],
                "history_revisions": len(out["history"])})
    out["history"] = out["history"][-1:]
    return out


def record_replay():
    with tempfile.TemporaryDirectory(prefix="transitos-replay-") as temp:
        session = DemoSession(StubReasoner(), IncidentMemory(Path(temp) / "memory.sqlite"))
        try:
            nodes = {"ready": clean_snapshot(session.state())}

            def step(action, name):
                state = session.state()
                run = next(iter(state["runs"]), {})
                command = Command(action=action, generation=session.generation,
                    plan_version=run.get("plan_version"), user="Demo dispatcher",
                    scope="return_to_service" if run.get("status") == "RETURNING" else "plan")
                session.validate(command)
                session.perform(command)
                state = session.state()
                if state["error"] or state["invariant_violations"]:
                    raise ValueError(f"Replay capture failed at {name}")
                nodes[name] = clean_snapshot(state)

            for action, name in (("trigger", "proposal"), ("approve", "approved"), ("advance", "alert_visible"),
                                 ("repair", "return"), ("approve", "done")):
                step(action, name)
            session.reset()
            step("trigger", "proposal_reject_branch")
            step("reject", "rejected")
            session.reset()
            step("trigger", "proposal_direct_branch")
            step("approve", "approved_direct_branch")
            step("repair", "return_direct")
            step("approve", "done_direct")
            # Branch setup states are not separately presented; their common prefix is equivalent.
            for name in ("proposal_reject_branch", "proposal_direct_branch", "approved_direct_branch"):
                nodes.pop(name)
            return {"schema_version": 1, "mode": "recorded_replay", "scenario": "operator_swap_needed",
                "provenance": "Fresh deterministic capture of DemoSession over the actual integrated System; no model calls.",
                "states": nodes, "edges": {"ready": {"trigger": "proposal"},
                    "proposal": {"approve": "approved", "reject": "rejected"},
                    "approved": {"advance": "alert_visible", "repair": "return_direct"},
                    "alert_visible": {"repair": "return"}, "return": {"approve": "done"},
                    "return_direct": {"approve": "done_direct"}, "done": {}, "done_direct": {}, "rejected": {}}}
        finally:
            session.close()


def source_guide():
    modules = []
    for ident, title, relative, detail, symbol in SOURCES:
        path = ROOT / relative
        text = path.read_text()
        lines = text.splitlines()
        line = next((i + 1 for i, value in enumerate(lines) if f"def {symbol}(" in value or f"class {symbol}" in value), 1)
        modules.append({"id": ident, "title": title, "path": relative, "description": detail,
            "line": line, "sha256": hashlib.sha256(text.encode()).hexdigest(),
            "excerpt": "\n".join(lines[max(0, line - 3):line + 35]),
            "url": f"https://github.com/kranthi7899/transitOS_poc_clean/blob/main/{relative}#L{line}"})
    return modules


def build(output: Path):
    output.mkdir(parents=True, exist_ok=True)
    template = ROOT / "docs" / "explorer"
    for path in template.iterdir():
        if path.suffix in (".html", ".css", ".js"):
            shutil.copy2(path, output / path.name)
    demo = output / "demo"
    demo.mkdir(exist_ok=True)
    assets = ROOT / "src" / "transitos" / "panel" / "console"
    shutil.copytree(assets, demo / "assets", dirs_exist_ok=True, ignore=shutil.ignore_patterns("*.html"))
    (demo / "assets" / "index.html").unlink(missing_ok=True)  # obsolete generated copy, never a source file
    html = (assets / "index.html").read_text().replace('<html lang="en">', '<html lang="en" data-mode="replay">')
    html = html.replace('/assets/', './assets/').replace('href="/"', 'href="../index.html"')
    html = html.replace('<main>', '<main><div class="notice replay-notice">Recorded replay of an actual integrated run. Controls select captured states; no live approvals, product writes or model calls happen here. <a href="../index.html">Back to repository explorer</a>.</div>')
    (demo / "index.html").write_text(html)
    product_html = (assets / "product.html").read_text().replace('<html lang="en">', '<html lang="en" data-mode="replay">')
    product_html = product_html.replace('/assets/', './assets/').replace('href="/"', 'href="./index.html"')
    product_html = product_html.replace('READ-ONLY LIVE VIEW', 'RECORDED REPLAY VIEW')
    product_html = product_html.replace("Keep this tab open beside the dispatcher to see updates.",
        "This page follows captured dispatcher replay states in this browser; no live product calls happen here.")
    (demo / "product.html").write_text(product_html)
    (demo / "replay.json").write_text(json.dumps(record_replay(), separators=(",", ":")) + "\n")
    eval_path = ROOT / "eval-report.json"
    report = json.loads(eval_path.read_text()) if eval_path.is_file() else None
    sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    data = {"name": "TransitOS", "base_commit": sha, "modules": source_guide(), "evals": report,
            "scope": "Simulated products, Level 2 approvals, one-scenario dispatcher console; zero paid calls in this explorer."}
    (output / "guide.json").write_text(json.dumps(data, indent=2) + "\n")
    (output / ".nojekyll").write_text("")
    return data


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=ROOT / "site")
    args = parser.parse_args()
    result = build(args.out)
    print(f"Explorer built at {args.out}: {len(result['modules'])} source entries, actual-system replay, no API calls")
