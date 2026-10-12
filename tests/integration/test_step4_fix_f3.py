"""Step 4 fix round F3 (A146): epoch milliseconds outside 2000-01-01T00:00Z (inclusive) .. 2100-01-01T00:00Z
(exclusive) are refused with a ValueError naming the value (seconds-sized, nanosecond-sized, 0, negative); an
OverflowError is never let out. The webhook answers 400; the backfill poller rejects that item and emits the rest of
the page (bug sweep B, A155; it used to hold the whole page with last_error)."""
from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from transitos.plumbing import Db, Relay, SqliteBus
from transitos.plumbing.watch import TelematicsBackfillPoller, WebhookReceiver
from transitos.plumbing.watch.base import list_rejects
from transitos.plumbing.watch.telematics import sign
from transitos.tools.common import epoch_ms_to_utc

from .conftest import AG, H

LO, HI = 946_684_800_000, 4_102_444_800_000          # 2000-01-01T00:00Z, 2100-01-01T00:00Z in ms


@pytest.mark.parametrize("bad", [1_793_500_200, 1_793_500_200_000_000_000, 10**30, 0, -1, -1_793_500_200_000,
                                 LO - 1, HI])
def test_out_of_range_epoch_values_are_refused_with_the_value(bad):
    with pytest.raises(ValueError, match=str(bad)):
        epoch_ms_to_utc(bad)


@pytest.mark.parametrize(("ok", "want"), [(LO, datetime(2000, 1, 1, tzinfo=UTC)),
                                          (HI - 1, datetime(2099, 12, 31, 23, 59, 59, 999000, tzinfo=UTC)),
                                          (1_793_500_200_000, datetime(2026, 11, 1, 2, 30, tzinfo=UTC))])
def test_in_range_boundaries(ok, want):
    assert epoch_ms_to_utc(ok) == want


def _payload(t) -> dict:
    return {"eventId": f"e-{t}", "vehicle": {"id": "T-77"}, "fault": {"spn": 110, "fmi": 0}, "time": t}


@pytest.mark.parametrize("bad", [1_793_500_200, 1_793_500_200_000_000_000, 0, -5])
def test_webhook_answers_400_for_an_implausible_epoch(tmp_path, bad):
    rec = WebhookReceiver(Db(str(tmp_path / "w.db")), AG)
    body = json.dumps(_payload(bad)).encode()
    code, out = rec.handle(body, sign("dev-secret", body))
    assert code == 400 and str(bad) in out["error"]


@pytest.mark.parametrize("bad", [1_793_500_200, 1_793_500_200_000_000_000])
def test_backfill_rejects_the_bad_item_and_emits_the_rest_instead_of_crashing(tmp_path, mocks, bad):
    """Bug sweep B (F7-R4, A155) DELIBERATELY changed this expectation: the page is no longer held. The item with the
    implausible epoch is rejected (a poll_rejects row naming the value, never emitted), the good item on the same
    page is emitted, and last_error stays None (the poll itself ran)."""
    db = Db(str(tmp_path / "b.db"))
    t = mocks.c("telematics")
    t.post("/sim/fault", json={"vehicle": "T-77", "spn": 110, "fmi": 0}, headers=H)
    st = mocks.apps["telematics"].state.ctx.store
    for e in st.all(AG, "events"):                     # the vendor sends a broken time on this item
        e["payload"]["time"] = bad
        st.put(AG, "events", f"{e['seq']:08d}", e)
    t.post("/sim/fault", json={"vehicle": "T-78", "spn": 110, "fmi": 0}, headers=H)   # a good item, same page
    p = TelematicsBackfillPoller(db, t, AG)
    assert p.poll() == 1 and p.last_error is None
    (row,) = list_rejects(db)
    assert row["product"] == "telematics" and str(bad) in row["reason"]
    bus = SqliteBus(db)
    Relay(db, bus).drain()
    assert [e.data["native_vehicle_id"] for _, _, e in bus.messages("events.raw")] == ["T-78"]
