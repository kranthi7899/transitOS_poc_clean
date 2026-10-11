"""Step 3.4 detection latency. Rows written from the spec: truth_to_telematics_s, truth_to_first_proposal_s,
truth_to_pull_s, in simulated seconds; a measurement that cannot be made is None, never 0."""
from datetime import UTC, datetime, timedelta

from transitos.verification.latency import detection_latency
from transitos.verification.model import AuditEvent
from transitos.world.engine import Observation

DAY = datetime(2026, 10, 10, tzinfo=UTC)


def at(h, m=0, s=0):
    return DAY + timedelta(hours=h, minutes=m, seconds=s)


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


TELE = Observation("telematics", "veh-3", "fault_on", at(9, 40), at(9, 40, 5))
SHOP = Observation("maintenance", "veh-3", "fault_on", at(9, 40), at(10, 0))
PROPOSAL = AuditEvent(seq=1, kind="proposal", run_id="r1", data={"at": iso(at(9, 40, 5))})
PULL = AuditEvent(seq=2, kind="write", run_id="r1", key="r1:set_vehicle_unavailable:1",
                  data={"action": "set_vehicle_unavailable", "at": iso(at(9, 41))})


def test_full_chain_in_simulated_seconds():
    assert detection_latency([SHOP, TELE], [PULL, PROPOSAL]) == {
        "truth_to_telematics_s": 5.0, "truth_to_first_proposal_s": 5.0, "truth_to_pull_s": 60.0}


def test_missing_stages_are_none_not_zero():
    assert detection_latency([TELE], []) == {
        "truth_to_telematics_s": 5.0, "truth_to_first_proposal_s": None, "truth_to_pull_s": None}
    assert detection_latency([SHOP], [PROPOSAL, PULL]) == {          # telematics never saw it: unknown
        "truth_to_telematics_s": None, "truth_to_first_proposal_s": 5.0, "truth_to_pull_s": 60.0}
    assert detection_latency([], [PROPOSAL, PULL]) == {
        "truth_to_telematics_s": None, "truth_to_first_proposal_s": None, "truth_to_pull_s": None}


def test_only_the_first_pull_and_a_proposal_after_the_truth_count():
    early = AuditEvent(seq=0, kind="proposal", run_id="r0", data={"at": iso(at(8))})       # an earlier, unrelated run
    comp = AuditEvent(seq=3, kind="write", run_id="r1", data={"action": "set_vehicle_unavailable", "at": iso(at(9, 50)),
                                                              "compensation": True})
    later = AuditEvent(seq=4, kind="write", run_id="r1", data={"action": "set_vehicle_unavailable", "at": iso(at(9, 59))})
    other = AuditEvent(seq=5, kind="write", run_id="r1", data={"action": "create_work_order", "at": iso(at(9, 40, 30))})
    got = detection_latency([TELE], [early, other, comp, later, PULL, PROPOSAL])
    assert got["truth_to_first_proposal_s"] == 5.0 and got["truth_to_pull_s"] == 60.0


def test_uses_the_earliest_truth_when_several_faults():
    t2 = Observation("telematics", "veh-1", "fault_on", at(14), at(14, 0, 5))
    assert detection_latency([t2, TELE], [PROPOSAL])["truth_to_telematics_s"] == 5.0
