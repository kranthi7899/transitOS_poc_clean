"""Bug sweep B round 3 (R4 R1, A175-A177), through the REAL pollers and the REAL mock products. Expected rows from
the ruling text.

A stale reject used to pin its stream's floor forever (every poll re-read everything since it). Now:
(a) DISMISS: an operator dismisses a reject (panel POST /poll-rejects/{id}/dismiss, or the Dismiss button). The row is
    marked `dismissed` (never deleted), an append-only audit row records who / when (SIM time) / why, and the floor is
    released in the same transaction; the cursor then moves on and the per-poll cost drops back. A dismissed record the
    product sends again UNCHANGED stays dismissed (no new reject, no floor); a CHANGED one is re-evaluated (A176).
(b) SUPERSEDE: a scheduling / workforce change that is followed, for the same vehicle / operator, by a change that was
    read successfully is marked `superseded` and its floor released."""
from __future__ import annotations

from typing import Any

import httpx
import pytest

from transitos.plumbing import Db, Relay, SqliteBus
from transitos.plumbing.watch import SchedulingPoller, WorkforcePoller
from transitos.plumbing.watch.base import (
    dismiss_reject,
    list_rejects,
    load_state,
    reject_audit,
)

from .conftest import AG, H

STALE = {"vehicle": "BUS-55", "status": "out_of_service", "changed_at": "not a time", "changed_by": "x"}


@pytest.fixture
def db(tmp_path):
    return Db(str(tmp_path / "b.db"))


def raw(db):
    bus = SqliteBus(db)
    Relay(db, bus).drain()
    return [e for _, _, e in bus.messages("events.raw")]


class Feed:
    """The real scheduling / workforce client, counting the records each change-feed read returns, with extra records
    the 'product' also sends (a stale malformed one, say) injected at the front or the back of every page."""

    def __init__(self, inner: Any, path: str) -> None:
        self.inner, self.path = inner, path
        self.front: list[dict] = []
        self.back: list[dict] = []
        self.counts: list[int] = []

    def get(self, path: str, **kw: Any):
        r = self.inner.get(path, **kw)
        if path != self.path:
            return r
        docs = [*self.front, *r.json(), *self.back]
        self.counts.append(len(docs))
        return httpx.Response(200, json=docs, request=httpx.Request("GET", path))

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


def _changes(mocks, n: int) -> None:
    s = mocks.c("scheduling")
    for i in range(n):
        mocks.clock.advance(minutes=1)
        s.post("/sim/vehicle-status", json={"vehicle": "BUS-45" if i % 2 else "BUS-46",
                                            "status": "out_of_service" if i % 4 < 2 else "in_service",
                                            "changed_by": "dispatcher"}, headers=H)


# ------------------------------------------------------------------ (a) dismiss
def test_a_stale_reject_costs_every_poll_until_dismissed_then_the_cost_drops_back(db, mocks):
    _changes(mocks, 50)
    feed = Feed(mocks.c("scheduling"), "/status-changes")
    feed.front.append(dict(STALE))                                     # a stale malformed record, never corrected
    p = SchedulingPoller(db, feed, AG)
    p.clock = mocks.clock.now
    for _ in range(3):
        p.poll()
    assert feed.counts == [51, 51, 51]                                 # the floor pins the cursor: all re-read
    (row,) = list_rejects(db)
    assert row["state"] == "open"
    mocks.clock.advance(minutes=5)
    out = dismiss_reject(db, row["id"], by="ops@example", reason="stale test record", at=mocks.clock.now().isoformat())
    assert out is not None and out["state"] == "dismissed"
    assert "reject_floors" not in load_state(db, p.name)[1]            # released in the same transaction
    (audit,) = reject_audit(db)
    assert (audit["action"], audit["by_user"], audit["reason"], audit["at"], audit["reject_id"]) == (
        "dismissed", "ops@example", "stale test record", mocks.clock.now().isoformat(), row["id"])
    p.poll()
    p.poll()
    p.poll()
    assert feed.counts[3:] == [51, 2, 2]                               # one catch-up read, then the cursor moves on
    assert list_rejects(db) == []                                      # the stale record re-sent unchanged: no new reject
    (kept,) = list_rejects(db, include_resolved=True)                  # never deleted
    assert (kept["id"], kept["state"]) == (row["id"], "dismissed")
    assert "reject_floors" not in load_state(db, p.name)[1]
    assert len(raw(db)) == 50                                          # every good change emitted exactly once


def test_a_dismissal_made_while_a_poll_is_reading_is_not_undone_by_that_poll(db, mocks):
    """The race: the poll loaded its state (with the floor), then the operator dismisses, then the same poll re-reads
    the unchanged stale record and writes its state. The row stays dismissed and the floor stays released."""
    _changes(mocks, 5)
    feed = Feed(mocks.c("scheduling"), "/status-changes")
    feed.front.append(dict(STALE))
    p = SchedulingPoller(db, feed, AG)
    p.poll()
    (row,) = list_rejects(db)
    inner_get = feed.get

    def get_and_dismiss(path, **kw):
        if path == "/status-changes":
            dismiss_reject(db, row["id"], by="ops", reason="during the poll", at=None)
        return inner_get(path, **kw)
    feed.get = get_and_dismiss   # type: ignore[method-assign]
    p.poll()
    (kept,) = list_rejects(db, include_resolved=True)
    assert kept["state"] == "dismissed" and "reject_floors" not in load_state(db, p.name)[1]


def test_dismissing_an_unknown_id_is_none_and_changes_nothing(db, mocks):
    p = SchedulingPoller(db, mocks.c("scheduling"), AG)
    p.poll()
    assert dismiss_reject(db, 999, by="ops", reason="x", at=None) is None
    assert reject_audit(db) == []


def test_a_dismissed_record_that_changes_and_is_still_unreadable_is_a_new_open_reject(db, mocks):
    feed = Feed(mocks.c("scheduling"), "/status-changes")
    feed.front.append(dict(STALE))
    p = SchedulingPoller(db, feed, AG)
    p.clock = mocks.clock.now
    p.poll()
    (row,) = list_rejects(db)
    dismiss_reject(db, row["id"], by="ops", reason="noise", at=mocks.clock.now().isoformat())
    feed.front[0] = {**STALE, "changed_by": "someone else"}            # the product CHANGED it, still unreadable
    mocks.clock.advance(minutes=1)
    p.poll()
    (again,) = list_rejects(db)
    assert again["id"] == row["id"] and again["state"] == "open"       # the same row, reopened
    assert [a["action"] for a in reject_audit(db)] == ["dismissed", "reopened"]
    assert load_state(db, p.name)[1]["reject_floors"]                  # it holds the cursor again


def test_the_reject_audit_is_append_only(db, mocks):
    import sqlite3
    feed = Feed(mocks.c("scheduling"), "/status-changes")
    feed.front.append(dict(STALE))
    p = SchedulingPoller(db, feed, AG)
    p.poll()
    dismiss_reject(db, list_rejects(db)[0]["id"], by="ops", reason="noise", at=None)
    for sql in ("UPDATE poll_reject_audit SET by_user='x'", "DELETE FROM poll_reject_audit"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute(sql)
    assert len(reject_audit(db)) == 1


def test_dismissing_twice_is_idempotent(db, mocks):
    feed = Feed(mocks.c("scheduling"), "/status-changes")
    feed.front.append(dict(STALE))
    p = SchedulingPoller(db, feed, AG)
    p.poll()
    (row,) = list_rejects(db)
    a = dismiss_reject(db, row["id"], by="ops", reason="noise", at=None)
    b = dismiss_reject(db, row["id"], by="ops2", reason="again", at=None)
    assert a["state"] == b["state"] == "dismissed" and len(reject_audit(db)) == 1


# ------------------------------------------------------------------ (b) supersede
def test_scheduling_a_later_change_for_the_same_vehicle_supersedes_the_rejected_one(db, mocks):
    mocks.clock.advance(minutes=5)
    good = mocks.c("scheduling").post("/sim/vehicle-status", json={"vehicle": "BUS-44", "status": "out_of_service",
                                                                   "changed_by": "dispatcher"}, headers=H).json()
    feed = Feed(mocks.c("scheduling"), "/status-changes")
    feed.front.append({**STALE, "vehicle": "BUS-44"})                  # the bad one comes first in the feed
    p = SchedulingPoller(db, feed, AG)
    p.clock = mocks.clock.now
    assert p.poll() == 1 and good["id"]
    (row,) = list_rejects(db, include_resolved=True)
    assert row["state"] == "superseded" and list_rejects(db) == []
    assert "reject_floors" not in load_state(db, p.name)[1]
    (audit,) = reject_audit(db)
    assert audit["action"] == "superseded" and "change:BUS-44:" in audit["reason"] and audit["by_user"] == "system"
    assert load_state(db, p.name)[0] is not None                       # the cursor moved


def test_scheduling_a_change_for_another_vehicle_or_before_it_does_not_supersede(db, mocks):
    mocks.clock.advance(minutes=5)
    for v in ("BUS-44", "BUS-45"):
        mocks.c("scheduling").post("/sim/vehicle-status", json={"vehicle": v, "status": "out_of_service",
                                                                "changed_by": "dispatcher"}, headers=H)
    feed = Feed(mocks.c("scheduling"), "/status-changes")
    feed.back.append({**STALE, "vehicle": "BUS-44"})                   # BUS-44's good change came BEFORE it
    feed.front.append({**STALE, "vehicle": "BUS-47"})                  # nothing later for BUS-47
    p = SchedulingPoller(db, feed, AG)
    assert p.poll() == 2
    assert sorted(r["native_id"] for r in list_rejects(db)) == ["BUS-44", "BUS-47"]
    assert reject_audit(db) == []


def test_workforce_a_later_change_for_the_same_operator_supersedes_the_rejected_one(db, mocks):
    mocks.clock.advance(minutes=5)
    mocks.c("workforce").post("/sim/operator", json={"operator": "OP-310", "status": "unavailable"}, headers=H)
    feed = Feed(mocks.c("workforce"), "/availability-changes")
    feed.front.append({"operator": "OP-310", "status": "unavailable", "changed_at": "not a time"})
    p = WorkforcePoller(db, feed, AG)
    assert p.poll() == 1
    (row,) = list_rejects(db, include_resolved=True)
    assert (row["native_id"], row["state"]) == ("OP-310", "superseded")
    assert "reject_floors" not in load_state(db, p.name)[1]


# ------------------------------------------------------------------ the integrated system: panel action
def test_panel_dismiss_and_404():
    from transitos.integration.world import make_world
    from transitos.verification.scenarios import BY_ID
    w = make_world()
    w.apply(BY_ID["scripted_day_coolant"])
    s = w.system
    feed = Feed(s.pollers[1].client, "/status-changes")                # the scheduling poller
    feed.front.append(dict(STALE))
    s.pollers[1].client = feed
    s.run_until_idle()
    (item,) = s.panel_client.get("/poll-rejects", params={"agency": "agency-a"}).json()
    page = s.panel_client.get("/ui/unresolved").text
    assert f"/ui/poll-rejects/{item['id']}/dismiss" in page
    assert s.panel_client.post("/poll-rejects/99999/dismiss", json={"user": "ops", "reason": "x"}).status_code == 404
    r = s.panel_client.post(f"/poll-rejects/{item['id']}/dismiss", json={"user": "ops", "reason": "stale"})
    assert r.status_code == 200 and r.json()["state"] == "dismissed"
    assert s.panel_client.get("/poll-rejects", params={"agency": "agency-a"}).json() == []
    (audit,) = reject_audit(s.db)
    assert audit["at"] == s.clock.now().isoformat() and audit["by_user"] == "ops"   # sim time
    r = s.panel_client.post(f"/ui/poll-rejects/{item['id']}/dismiss", data={"user": "ops", "reason": "again"},
                            follow_redirects=False)
    assert r.status_code == 303
