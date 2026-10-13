"""Read-only native mock-product evidence and dispatcher updates for the demo (A205)."""
from __future__ import annotations

import csv
import io

PRODUCTS = {
    "telematics": ("Signal Desk", "Telematics", "Signed webhook + JSON feed", "device / vehicle.id"),
    "maintenance": ("Shop Ledger", "Maintenance", "Polled REST JSON", "asset / wo_no"),
    "scheduling": ("Service Board", "Scheduling", "REST JSON + status changes", "vehicle / block"),
    "workforce": ("Crew Roster", "Workforce", "CSV export + assignment API", "operator / block"),
    "alerts": ("Rider Bulletin", "Rider communications", "GTFS-style alert JSON + visibility delay", "block / alert id"),
}


def read_products(world):
    """Read each existing application through its native interface; never create a second store."""
    result = {}
    paths = {"telematics": ("/vehicles", "/faults/current"),
             "maintenance": ("/assets", "/workorders"),
             "scheduling": ("/vehicles", "/blocks", "/status-changes"),
             "workforce": ("/export/operators.csv", "/assignments/blk-1"), "alerts": ("/alerts",)}
    for key, endpoints in paths.items():
        name, role, wire, identity = PRODUCTS[key]
        records = {}
        for path in endpoints:
            response = world.mocks.clients[key].get(path, headers=world._h)
            if response.status_code != 200:
                records[path] = {"unavailable": True, "http_status": response.status_code}
            elif path.endswith(".csv"):
                records[path] = {"format": "text/csv", "rows": list(csv.DictReader(io.StringIO(response.text))),
                                 "raw": response.text}
            else:
                records[path] = response.json()
        result[key] = {"name": name, "role": role, "format": wire, "identity": identity,
                       "records": records, "captured_at": world.system.clock.iso()}
    return result


def update_notifications(session, data):
    """Deduplicate evidence-backed milestones; this is an in-app feed, not external delivery."""
    def add(key, title, detail, product="workflow", severity="info"):
        if key not in session.notification_keys:
            session.notification_keys.add(key)
            session.notifications.append({"id": len(session.notifications) + 1, "key": key,
                "title": title, "detail": detail, "product": product, "severity": severity,
                "time": session.system.clock.iso()})

    run = next(iter(data["runs"]), None)
    if not run:
        return
    add("fault", "Vehicle fault received", "T-77 / BUS-44: signed telematics event started the workflow.", "telematics")
    messages = {"gather": ("Product evidence gathered", "Native records resolved for this incident."),
                "check": ("Safety checks evaluated", "Code evaluates current facts; the model cannot relax hard limits."),
                "reason": ("Recovery proposal prepared", "Read the returned rationale and model/fallback label in Operations.")}
    for entry in session.trace:
        if entry["node"] in messages:
            title, detail = messages[entry["node"]]
            add(f"node:{entry['node']}:{run['plan_version']}", title, detail)
    for event in data["audit"]:
        if event["kind"] in ("approval", "rejection"):
            scope = event["data"].get("scope", "plan")
            add(f"audit:{event['seq']}", f"{scope.replace('_', ' ').capitalize()} {event['kind']}",
                f"Decision recorded by {event['data'].get('by', 'Dispatcher')}.", severity="decision")
    messages = {"create_work_order": ("Maintenance work order opened", "Repair is tracked in the maintenance application.", "maintenance"),
        "set_vehicle_unavailable": ("BUS-44 held out of service", "Scheduling records the faulty vehicle as unavailable.", "scheduling"),
        "reassign_block": ("Replacement bus assignment confirmed", "Scheduling has changed the block assignment. Physical arrival is not tracked.", "scheduling"),
        "assign_operator": ("Replacement operator assigned", "The workforce assignment record changed.", "workforce"),
        "publish_rider_alert": ("Rider alert published", "Publication is confirmed; feed visibility has a separate delay.", "alerts"),
        "set_vehicle_available": ("BUS-44 returned to availability", "Vehicle roster is ready again; the block stays on its replacement.", "scheduling")}
    for write in data["writes"]:
        if write["ok"] and not write["compensation"] and write["action"] in messages:
            title, detail, product = messages[write["action"]]
            add(f"write:{write['key']}", title, detail, product)
        elif not write["ok"]:
            add(f"write:{write['key']}", "Product action refused or failed", "Review the audit and current product records.", severity="warning")
    if data["visible_alerts"]:
        add("alert-visible", "Rider alert visible in feed", "The delayed rider-facing feed now includes the published alert.", "alerts")
    if any(str(w["status"]).lower() == "closed" for w in data["work_orders"]):
        add("repair-closed", "Maintenance confirmed completed repair", "Work order closed. Scheduling remains out of service until return approval.", "maintenance")
    stages = {"AWAITING_APPROVAL": ("Recovery approval needed", "Zero recovery writes until the dispatcher approves this version."),
              "WAITING_FOR_REPAIR": ("Workflow waiting for repair", "Replacement assignment is confirmed; the graph waits for a maintenance event."),
              "RETURNING": ("Return-to-service approval needed", "Repair completion does not authorize availability restoration."),
              "DONE": ("Incident response complete", "Both approval gates completed. Review product records and the audit."),
              "CLOSED_REJECTED": ("Recovery proposal rejected", "No automatic recovery follows this decision."),
              "ESCALATED": ("Dispatcher intervention needed", "Review the exact state and unresolved evidence.")}
    if run["status"] in stages:
        title, detail = stages[run["status"]]
        add(f"status:{run['status']}:{run['plan_version']}", title, detail,
            severity="decision" if run["status"] in ("AWAITING_APPROVAL", "RETURNING", "ESCALATED") else "info")
