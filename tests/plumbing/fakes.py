"""Minimal local fakes following docs/MOCK_API.md (Track A mocks not on main yet). State is a plain dict."""
from __future__ import annotations

from fastapi import FastAPI


def maintenance_app(state: dict) -> FastAPI:
    app = FastAPI()

    def since(items, updated_since):
        return [i for i in items if not updated_since or i["updated_at"] >= updated_since]

    @app.get("/pm-schedules")
    def pm(updated_since: str | None = None):
        return since(state.setdefault("pm", []), updated_since)

    @app.get("/workorders")
    def wos(updated_since: str | None = None):
        return since(state.setdefault("wos", []), updated_since)

    return app


def scheduling_app(state: dict) -> FastAPI:
    app = FastAPI()

    @app.get("/status-changes")
    def sc(since: str | None = None):
        return [c for c in state.setdefault("changes", []) if not since or c["changed_at"] > since]

    return app


def workforce_app(state: dict) -> FastAPI:
    app = FastAPI()

    @app.get("/availability-changes")
    def ac(since: str | None = None):
        return [c for c in state.setdefault("changes", []) if not since or c["changed_at"] > since]

    return app


def telematics_app(state: dict) -> FastAPI:
    app = FastAPI()

    @app.get("/faults/feed")
    def feed(cursor: str | None = None):
        items = state.setdefault("feed", [])
        start = int(cursor or 0)
        return {"items": items[start:], "next_cursor": str(len(items))}

    return app


def fault_payload(event_id="evt-1", vehicle="T-77", spn=100, fmi=1, time="2026-01-01T10:00:00Z") -> dict:
    return {"eventId": event_id, "vehicle": {"id": vehicle, "name": "Bus"},
            "fault": {"spn": spn, "fmi": fmi, "lamps": {"stop": True, "warn": False, "protect": False},
                      "occurrenceCount": 1}, "time": time}
