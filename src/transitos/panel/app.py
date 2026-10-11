"""Approval panel: FastAPI JSON API + server-rendered, phone-friendly HTML (htmx for progressive enhancement)."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from html import escape
from typing import Any, Literal
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from transitos.contracts import ids
from transitos.contracts.crosswalk import Crosswalk, CrosswalkEntry
from transitos.contracts.envelope import CloudEvent
from transitos.contracts.objects import RunState

from .ports import (
    AuditReader,
    DecisionConflict,
    EventSink,
    KillSwitchStore,
    RunGateway,
    RunNotFound,
    StaleVersion,
    UnresolvedReader,
)


class DecisionBody(BaseModel):
    decision: Literal["approve", "reject", "edit"]
    plan_version: int
    edits: dict[str, Any] | None = None
    user: str
    scope: Literal["plan", "return_to_service"] = "plan"  # return_to_service: the separate return approval
    acknowledged: list[str] = []  # clearance rule ids the approver acknowledges (Phase 10, amendment A)


class DefectBody(BaseModel):
    agency: str
    native_vehicle_id: str
    defect_text: str
    reported_by_role: str = "operator"
    user: str = ""
    spn: int | None = None  # with fmi: emit VehicleFaultDetected(source=human) instead of DefectReported
    fmi: int | None = None


class KillSwitchBody(BaseModel):
    agency: str
    on: bool


def _summary(run: RunState) -> dict[str, Any]:
    return {
        "run_id": run.run_id, "agency": run.agency, "vehicle_id": run.vehicle_id,
        "status": run.status, "plan_version": run.plan_version,
        "decision": run.plan.decision if run.plan else None,
        "reasoning": run.plan.reasoning if run.plan else "",
        "needs_acknowledgement": sorted({i.rule for i in run.plan.clearance if i.requires_ack}
                                        - set(run.acknowledged)) if run.plan else [],
    }


def build_defect_event(body: DefectBody, now: datetime | None = None) -> CloudEvent:
    now = (now or datetime.now(UTC)).replace(microsecond=0)
    ts = now.isoformat().replace("+00:00", "Z")
    h = hashlib.sha256(body.defect_text.encode()).hexdigest()[:12]
    source = f"adapter/panel/{body.agency}"
    if body.spn is not None and body.fmi is not None:
        return CloudEvent(
            id=ids.fault_id(body.agency, body.native_vehicle_id, body.spn, body.fmi, ts),
            source=source, type="VehicleFaultDetected", time=now, agencyid=body.agency,
            data={"native_vehicle_id": body.native_vehicle_id, "spn": body.spn, "fmi": body.fmi,
                  "source": "human", "defect_text": body.defect_text, "reported_by": body.user})
    return CloudEvent(
        id=ids.defect_id(body.agency, body.native_vehicle_id, ts, h),
        source=source, type="DefectReported", time=now, agencyid=body.agency,
        data={"native_vehicle_id": body.native_vehicle_id, "defect_text": body.defect_text,
              "reported_by_role": body.reported_by_role, "source": "human",
              "reported_by": body.user})


# ---------------------------------------------------------------- HTML helpers
CSS = """
*{box-sizing:border-box}body{font:16px/1.4 system-ui,sans-serif;margin:0;background:#f4f5f7;color:#1b1f24}
header{background:#12355b;color:#fff;padding:.6rem 1rem;display:flex;flex-wrap:wrap;gap:.8rem;align-items:center}
header a{color:#fff;text-decoration:none;font-weight:600}main{max-width:60rem;margin:0 auto;padding:1rem}
.card{background:#fff;border-radius:8px;padding:1rem;margin:0 0 .8rem;box-shadow:0 1px 2px #0002}
table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:.35rem;border-bottom:1px solid #ddd;vertical-align:top}
button,input,select,textarea{font:inherit;padding:.6rem .8rem;margin:.2rem 0;border-radius:6px;border:1px solid #889}
button{cursor:pointer;color:#fff;border:0;min-height:44px}.ok{background:#1a7f37}.no{background:#b42318}.ed{background:#9a6700}
.pill{padding:.1rem .5rem;border-radius:99px;background:#dde;font-size:.85rem}.warn{color:#b42318;font-weight:700}
pre{white-space:pre-wrap;word-break:break-word;background:#f0f1f4;padding:.5rem;border-radius:6px}
@media(min-width:40rem){.row{display:flex;gap:.5rem;flex-wrap:wrap}}
"""


def _page(title: str, body: str) -> HTMLResponse:
    nav = ("<a href='/ui/pending'>Pending</a><a href='/ui/unresolved'>Unresolved</a>"
           "<a href='/ui/killswitch'>Kill switch</a><a href='/ui/timeline'>Timeline</a>"
           "<a href='/ui/defect'>Report fault</a>")
    return HTMLResponse(
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{escape(title)} - TransitOS</title><style>{CSS}</style>"
        "<script src='https://unpkg.com/htmx.org@1.9.12' defer></script></head><body>"
        f"<header><strong>TransitOS (simulated)</strong>{nav}</header>"
        f"<main><h1>{escape(title)}</h1>{body}</main></body></html>")


def _e(v: Any) -> str:
    return escape(str(v))


def _clearance_card(run: RunState) -> str:
    """Every derived, assumed, unverifiable or not-evaluated rule with its basis, and what still needs the
    approver's acknowledgement."""
    items = run.plan.clearance if run.plan else []
    if not items:
        return "<div class='card'><h2>Clearance</h2>All rules passed on native data.</div>"
    rows = "".join(
        f"<tr><td>{_e(i.rule)}</td><td>{'<span class=warn>' if i.status in ('violation', 'unverifiable') else ''}"
        f"{_e(i.status)}{'</span>' if i.status in ('violation', 'unverifiable') else ''}</td>"
        f"<td>{_e(i.basis)}</td><td>{_e(i.detail)}</td>"
        f"<td>{'acknowledged' if i.rule in run.acknowledged else ('needs acknowledgement' if i.requires_ack else '')}"
        "</td></tr>" for i in items)
    return ("<div class='card'><h2>Clearance</h2><table><tr><th>Rule</th><th>Status</th><th>Basis</th>"
            f"<th>Detail</th><th>Approver</th></tr>{rows}</table></div>")


async def _form(request: Request) -> dict[str, str]:
    raw = (await request.body()).decode()
    return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}


async def _form_multi(request: Request) -> dict[str, list[str]]:
    raw = (await request.body()).decode()
    return parse_qs(raw, keep_blank_values=True)


def _missing_acks(run: RunState, acknowledged: list[str]) -> list[str]:
    """Rule ids with a requires_ack clearance item that neither this request nor the run has acknowledged."""
    if run.plan is None:
        return []
    need = {i.rule for i in run.plan.clearance if i.requires_ack}
    return sorted(need - set(acknowledged) - set(run.acknowledged))


def create_app(*, gateway: RunGateway, crosswalk: Crosswalk, events: EventSink,
               audit: AuditReader, killswitch: KillSwitchStore,
               unresolved: UnresolvedReader | None = None,
               agencies: list[str] | None = None,
               clock: Callable[[], datetime] | None = None) -> FastAPI:
    """clock: time source for panel-created events (the sim clock in the integrated system)."""
    app = FastAPI(title="TransitOS panel")
    unresolved = unresolved or (crosswalk if hasattr(crosswalk, "list_unresolved") else None)

    def do_return(run_id: str, body: DecisionBody) -> dict[str, Any]:
        run = gateway.get_run(run_id)
        if run is None:
            raise HTTPException(404, "run not found")
        if body.decision == "edit":
            raise HTTPException(422, "return-to-service takes approve or reject")
        if run.status == "DONE":
            return {**_summary(run), "already_decided": True}
        if run.status != "RETURNING":
            raise HTTPException(409, f"run is not awaiting return-to-service approval (status {run.status})")
        try:
            run = gateway.submit_return(run_id, body.decision, body.user)
        except RunNotFound:
            raise HTTPException(404, "run not found")
        except (StaleVersion, DecisionConflict) as exc:
            raise HTTPException(409, str(exc) or type(exc).__name__)
        return {**_summary(run), "already_decided": False}

    def do_decision(run_id: str, body: DecisionBody) -> dict[str, Any]:
        if body.scope == "return_to_service":
            return do_return(run_id, body)
        run = gateway.get_run(run_id)
        if run is None:
            raise HTTPException(404, "run not found")
        if run.decision is not None:
            if run.decision == body.decision and run.plan_version == body.plan_version:
                return {**_summary(run), "already_decided": True}  # idempotent double submit
            raise HTTPException(409, "run already decided")
        if body.plan_version != run.plan_version:
            raise HTTPException(409, f"stale plan_version: current is {run.plan_version}")
        if body.decision == "approve" and (missing := _missing_acks(run, body.acknowledged)):
            raise HTTPException(409, "acknowledgement required for: " + ", ".join(missing))
        try:
            run = gateway.submit_decision(run_id, body.decision, body.plan_version,
                                          body.edits, body.user, body.acknowledged)
        except RunNotFound:
            raise HTTPException(404, "run not found")
        except (StaleVersion, DecisionConflict) as exc:
            raise HTTPException(409, str(exc) or type(exc).__name__)
        return {**_summary(run), "already_decided": False}

    def do_crosswalk(entry: CrosswalkEntry) -> dict[str, Any]:
        released = crosswalk.resolve(entry)
        for ev in released:
            events.publish(ev)
        return {"republished": [e.id for e in released]}

    # ------------------------------------------------------------ JSON API
    @app.get("/pending")
    def pending(agency: str | None = None):
        return [_summary(r) for r in gateway.list_pending(agency)]

    @app.get("/runs/{run_id:path}")
    def run_detail(run_id: str):
        run = gateway.get_run(run_id)
        if run is None:
            raise HTTPException(404, "run not found")
        return {
            "run": json.loads(run.model_dump_json()),
            "plan": json.loads(run.plan.model_dump_json()) if run.plan else None,
            "reasoning": run.plan.reasoning if run.plan else "",
            "facts": run.facts,
            "history": [json.loads(r.model_dump_json()) for r in audit.for_run(run_id)],
        }

    @app.post("/runs/{run_id:path}/decision")
    def decision(run_id: str, body: DecisionBody):
        return do_decision(run_id, body)

    @app.post("/defects", status_code=202)
    def defects(body: DefectBody):
        ev = build_defect_event(body, clock() if clock else None)
        events.publish(ev)
        return {"event_id": ev.id, "type": ev.type, "source": ev.data["source"]}

    @app.get("/unresolved")
    def unresolved_list(agency: str | None = None):
        return unresolved.list_unresolved(agency) if unresolved else []

    @app.post("/crosswalk")
    def crosswalk_fix(entry: CrosswalkEntry):
        return do_crosswalk(entry)

    @app.get("/killswitch")
    def killswitch_get(agency: str):
        return {"agency": agency, "on": killswitch.is_on(agency)}

    @app.post("/killswitch")
    def killswitch_set(body: KillSwitchBody):
        killswitch.set(body.agency, body.on)
        return {"agency": body.agency, "on": killswitch.is_on(body.agency)}

    @app.get("/timeline")
    def timeline(agency: str | None = None, limit: int = 100):
        return audit.timeline(agency, limit)

    # ------------------------------------------------------------ HTML pages
    @app.get("/", response_class=HTMLResponse)
    def index():
        return RedirectResponse("/ui/pending")

    @app.get("/ui/pending", response_class=HTMLResponse)
    def ui_pending(agency: str | None = None):
        rows = "".join(
            f"<tr><td><a href='/ui/runs/{_e(r.run_id)}'>{_e(r.vehicle_id)}</a></td>"
            f"<td>{_e(r.agency)}</td><td>{_e(r.plan.decision if r.plan else '-')}</td>"
            f"<td>v{r.plan_version}</td></tr>" for r in gateway.list_pending(agency))
        body = (f"<form method='get'><input name='agency' placeholder='agency' value='{_e(agency or '')}'>"
                "<button class='ed'>Filter</button></form>")
        body += ("<div class='card'><table><tr><th>Vehicle</th><th>Agency</th><th>Proposal</th>"
                 f"<th>Plan</th></tr>{rows}</table></div>" if rows
                 else "<div class='card'>No plans awaiting approval.</div>")
        return _page("Pending approvals", body)

    @app.get("/ui/runs/{run_id:path}", response_class=HTMLResponse)
    def ui_run(run_id: str):
        run = gateway.get_run(run_id)
        if run is None:
            raise HTTPException(404, "run not found")
        plan = run.plan
        b = (f"<div class='card'><span class='pill'>{_e(run.status)}</span> vehicle "
             f"<b>{_e(run.vehicle_id)}</b> agency {_e(run.agency)} plan v{run.plan_version}</div>")
        if run.violations:
            b += "<div class='card warn'>Violations: " + _e("; ".join(run.violations)) + "</div>"
        if plan:
            steps = "".join(f"<tr><td>{_e(s.action)}</td><td>{_e(s.params)}</td><td>{_e(s.status)}</td></tr>"
                            for s in plan.steps)
            fb = " <span class='warn'>(code-ranked fallback used)</span>" if plan.fallback_used else ""
            b += (f"<div class='card'><h2>Plan: {_e(plan.decision)}{fb}</h2>"
                  f"<p><b>Reasoning:</b> {_e(plan.reasoning)}</p>"
                  f"<table><tr><th>Step</th><th>Params</th><th>Status</th></tr>{steps}</table>"
                  f"<p><b>Assumptions:</b> {_e('; '.join(plan.assumptions) or '-')}</p>"
                  f"<p><b>Alternatives:</b> {_e(plan.alternatives or '-')}</p>"
                  f"<p><b>Ranking:</b> {_e(plan.rank_reason or '-')} "
                  f"(vehicle fit: {_e(plan.vehicle_fit or '-')})</p></div>")
            b += _clearance_card(run)
        b += f"<div class='card'><h2>Facts</h2><pre>{_e(json.dumps(run.facts, indent=2, default=str))}</pre></div>"
        hist = "".join(f"<tr><td>v{r.plan_version}</td><td>{_e(r.outcome)}</td>"
                       f"<td>{_e(r.human_action)}</td><td>{_e(r.actions)}</td></tr>"
                       for r in audit.for_run(run_id))
        b += ("<div class='card'><h2>Audit trail</h2><table><tr><th>Plan</th><th>Outcome</th>"
              f"<th>Human</th><th>Actions</th></tr>{hist}</table></div>")
        if run.status == "RETURNING":
            rid = _e(run_id)
            b += (f"<div class='card'><h2>Return to service</h2><form method='post' action='/ui/runs/{rid}/decision'>"
                  f"<input type='hidden' name='plan_version' value='{run.plan_version}'>"
                  "<input type='hidden' name='scope' value='return_to_service'>"
                  "<input name='user' placeholder='your name' required><div class='row'>"
                  "<button class='ok' name='decision' value='approve'>Approve return</button>"
                  "<button class='no' name='decision' value='reject'>Keep out of service</button>"
                  "</div></form></div>")
        elif run.decision is None:
            rid = _e(run_id)
            hidden = f"<input type='hidden' name='plan_version' value='{run.plan_version}'>"
            for rule in sorted({i.rule for i in (plan.clearance if plan else []) if i.requires_ack}):
                hidden += (f"<label><input type='checkbox' name='acknowledged' value='{_e(rule)}'> "
                           f"I acknowledge: {_e(rule)}</label><br>")
            btn = lambda d, cls, label: (
                f"<button class='{cls}' name='decision' value='{d}' "
                f"hx-post='/ui/runs/{rid}/decision' hx-include='closest form' "
                f"hx-target='#result'>{label}</button>")
            b += (f"<div class='card'><form method='post' action='/ui/runs/{rid}/decision'>{hidden}"
                  "<input name='user' placeholder='your name' required><br>"
                  "<textarea name='edits' rows='3' placeholder='Edits (JSON, for Edit only)' "
                  "style='width:100%'></textarea><div class='row'>"
                  f"{btn('approve', 'ok', 'Approve')}{btn('reject', 'no', 'Reject')}"
                  f"{btn('edit', 'ed', 'Edit')}</div></form><div id='result'></div></div>")
        else:
            b += (f"<div class='card'>Decision: <b>{_e(run.decision)}</b> by {_e(run.decided_by)}</div>")
        return _page("Run detail", b)

    @app.post("/ui/runs/{run_id:path}/decision", response_class=HTMLResponse)
    async def ui_decision(run_id: str, request: Request):
        f = await _form(request)
        acks = (await _form_multi(request)).get("acknowledged", [])
        try:
            edits = json.loads(f["edits"]) if f.get("edits", "").strip() else None
            body = DecisionBody(decision=f.get("decision", ""), plan_version=int(f.get("plan_version", "")),
                                edits=edits, user=f.get("user", ""), scope=f.get("scope") or "plan",
                                acknowledged=acks)
            res = do_decision(run_id, body)
            msg, code = f"Recorded {_e(body.decision)} (status {_e(res['status'])})", 200
        except HTTPException as exc:
            msg, code = f"<span class='warn'>Not applied: {_e(exc.detail)}</span>", exc.status_code
        except (ValueError, TypeError) as exc:
            msg, code = f"<span class='warn'>Invalid input: {_e(exc)}</span>", 422
        return HTMLResponse(f"<div class='card'>{msg}</div>", status_code=code)

    @app.get("/ui/unresolved", response_class=HTMLResponse)
    def ui_unresolved(agency: str | None = None):
        items = unresolved.list_unresolved(agency) if unresolved else []

        def hidden(i: dict) -> str:
            return (f"<input type='hidden' name='agency' value='{_e(i['agency'])}'>"
                    f"<input type='hidden' name='product' value='{_e(i['product'])}'>"
                    f"<input type='hidden' name='native_id' value='{_e(i['native_id'])}'>"
                    "<input type='hidden' name='entity' value='vehicle'>")

        def candidates(i: dict) -> str:   # Phase 10 Step 2: the resolver's candidates and evidence, one pick each
            out = "".join(
                f"<form method='post' action='/ui/crosswalk'>{hidden(i)}"
                f"<input type='hidden' name='canonical_id' value='{_e(c['canonical_id'])}'>"
                f"<button class='ok'>Pick {_e(c['canonical_id'])}</button> {float(c['score']):.2f} "
                f"<small>{_e('; '.join(c.get('evidence', [])))}</small></form>" for c in i.get("candidates") or [])
            return (f"<b>{_e(i['outcome'])}</b>" if i.get("outcome") else "") + out

        rows = "".join(
            f"<tr><td>{_e(i['agency'])}</td><td>{_e(i['product'])}</td><td>{_e(i['native_id'])}</td>"
            f"<td>{len(i['event_ids'])}</td><td>{candidates(i)}</td>"
            f"<td><form method='post' action='/ui/crosswalk'>{hidden(i)}"
            "<input name='canonical_id' placeholder='veh-12' required>"
            "<button class='ok'>Map</button></form></td></tr>" for i in items)
        body = ("<div class='card'><table><tr><th>Agency</th><th>Product</th><th>Native ID</th>"
                f"<th>Parked</th><th>Candidates</th><th>Fix</th></tr>{rows}</table></div>" if rows
                else "<div class='card'>Nothing unresolved.</div>")
        return _page("Unresolved IDs", body)

    @app.post("/ui/crosswalk")
    async def ui_crosswalk(request: Request):
        f = await _form(request)
        try:
            do_crosswalk(CrosswalkEntry(**{k: f.get(k, "") for k in
                                           ("agency", "product", "native_id", "entity", "canonical_id")}))
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        return RedirectResponse("/ui/unresolved", status_code=303)

    @app.get("/ui/killswitch", response_class=HTMLResponse)
    def ui_killswitch():
        rows = "".join(
            f"<div class='card'><b>{_e(a)}</b>: "
            f"{'<span class=warn>AGENT OFF</span>' if killswitch.is_on(a) else 'agent running'}"
            f"<form method='post' action='/ui/killswitch'><input type='hidden' name='agency' value='{_e(a)}'>"
            f"<input type='hidden' name='on' value='{'false' if killswitch.is_on(a) else 'true'}'>"
            f"<button class='no'>{'Resume agent' if killswitch.is_on(a) else 'Kill switch ON'}</button>"
            "</form></div>" for a in (agencies or []))
        extra = ("<div class='card'><form method='post' action='/ui/killswitch'>"
                 "<input name='agency' placeholder='agency id' required>"
                 "<input type='hidden' name='on' value='true'><button class='no'>Kill switch ON</button>"
                 "</form></div>")
        return _page("Kill switch", rows + extra)

    @app.post("/ui/killswitch")
    async def ui_killswitch_set(request: Request):
        f = await _form(request)
        killswitch.set(f.get("agency", ""), f.get("on") == "true")
        return RedirectResponse("/ui/killswitch", status_code=303)

    @app.get("/ui/timeline", response_class=HTMLResponse)
    def ui_timeline(agency: str | None = None):
        rows = "".join(
            f"<tr><td>{_e(t.get('time', ''))}</td><td>{_e(t.get('run_id', ''))}</td>"
            f"<td>{_e(t.get('kind', ''))}</td><td>{_e(t.get('summary', ''))}</td></tr>"
            for t in reversed(audit.timeline(agency, 100)))
        return _page("Event and audit timeline",
                     "<div class='card'><table><tr><th>Time</th><th>Run</th><th>Kind</th><th>Summary</th></tr>"
                     f"{rows}</table></div>")

    @app.get("/ui/defect", response_class=HTMLResponse)
    def ui_defect():
        return _page("Report a vehicle fault", (
            "<div class='card'><form method='post' action='/ui/defect'>"
            "<input name='agency' placeholder='agency' required><br>"
            "<input name='native_vehicle_id' placeholder='vehicle (e.g. T-77)' required><br>"
            "<textarea name='defect_text' rows='3' placeholder='What is wrong?' style='width:100%' required>"
            "</textarea><br><input name='user' placeholder='your name'><br>"
            "<button class='ok'>Submit</button></form></div>"))

    @app.post("/ui/defect", response_class=HTMLResponse)
    async def ui_defect_post(request: Request):
        f = await _form(request)
        try:
            body = DefectBody(agency=f.get("agency", ""), native_vehicle_id=f.get("native_vehicle_id", ""),
                              defect_text=f.get("defect_text", ""), user=f.get("user", ""))
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        ev = build_defect_event(body, clock() if clock else None)
        events.publish(ev)
        return _page("Report received", f"<div class='card'>Event {_e(ev.id)} queued.</div>")

    @app.exception_handler(HTTPException)
    async def _http_exc(request: Request, exc: HTTPException):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    return app
