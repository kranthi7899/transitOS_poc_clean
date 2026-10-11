"""Step 3.4 detection latency (fix round: decomposed, per vehicle, sub-second). Rows written from the spec:
truth_to_telematics_s, truth_to_first_proposal_s, truth_to_pull_s in simulated seconds, plus the human part split out
(truth_to_approval_s, approval_to_pull_s). A measurement that cannot be made is None, never 0. Proposal, approval and
pull are matched to the SAME vehicle's run as the fault."""
from datetime import UTC, datetime, timedelta

from transitos.verification.latency import detection_latency
from transitos.verification.model import AuditEvent
from transitos.world.engine import Observation
from transitos.world.script import Beat

DAY = datetime(2026, 10, 10, tzinfo=UTC)


def at(h, m=0, s=0):
    return DAY + timedelta(hours=h, minutes=m, seconds=s)


def iso(t):
    return t.isoformat()


def obs(veh, t, lag=5, product="telematics"):
    return Observation(product, veh, "fault_on", t, t + timedelta(seconds=lag))


def prop(veh, t, seq=1):
    return AuditEvent(seq=seq, kind="proposal", run_id="r-" + veh, data={"at": iso(t), "vehicle": veh})


def appr(veh, t, seq=2):
    return AuditEvent(seq=seq, kind="approval", run_id="r-" + veh, data={"at": iso(t), "vehicle": veh, "scope": "plan"})


def pull(veh, t, seq=3, **kw):
    return AuditEvent(seq=seq, kind="write", run_id="r-" + veh, key="k",
                      data={"action": "set_vehicle_unavailable", "at": iso(t), "vehicle": veh, **kw})


TELE = obs("veh-3", at(9, 40))
SHOP = obs("veh-3", at(9, 40), lag=1200, product="maintenance")
FLAT = ("truth_to_telematics_s", "truth_to_first_proposal_s", "truth_to_approval_s", "truth_to_pull_s", "approval_to_pull_s")


def flat(d):
    return {k: d[k] for k in FLAT}


def test_full_chain_in_simulated_seconds_with_the_human_part_split_out():
    got = detection_latency([SHOP, TELE], [pull("veh-3", at(9, 41)), appr("veh-3", at(9, 41)), prop("veh-3", at(9, 40, 5))])
    assert flat(got) == {"truth_to_telematics_s": 5.0, "truth_to_first_proposal_s": 5.0, "truth_to_approval_s": 60.0,
                         "truth_to_pull_s": 60.0, "approval_to_pull_s": 0.0}


def test_moving_the_approval_changes_the_human_part_not_the_proposal_time():
    a = detection_latency([TELE], [prop("veh-3", at(9, 40, 5)), appr("veh-3", at(9, 41)), pull("veh-3", at(9, 41))])
    b = detection_latency([TELE], [prop("veh-3", at(9, 40, 5)), appr("veh-3", at(9, 45)), pull("veh-3", at(9, 45))])
    assert a["truth_to_first_proposal_s"] == b["truth_to_first_proposal_s"] == 5.0
    assert (a["truth_to_approval_s"], b["truth_to_approval_s"]) == (60.0, 300.0)
    assert b["approval_to_pull_s"] == 0.0


def test_pull_after_a_later_write_step_is_decomposed():
    got = detection_latency([TELE], [prop("veh-3", at(9, 40, 5)), appr("veh-3", at(9, 41)), pull("veh-3", at(9, 41, 30))])
    assert (got["truth_to_approval_s"], got["approval_to_pull_s"], got["truth_to_pull_s"]) == (60.0, 30.0, 90.0)


def test_missing_stages_are_none_not_zero():
    assert flat(detection_latency([TELE], [])) == dict.fromkeys(FLAT) | {"truth_to_telematics_s": 5.0}
    got = detection_latency([SHOP], [prop("veh-3", at(9, 40, 5)), appr("veh-3", at(9, 41)), pull("veh-3", at(9, 41))])
    assert got["truth_to_telematics_s"] is None and got["truth_to_pull_s"] == 60.0       # telematics never saw it
    assert all(v is None for v in flat(detection_latency([], [prop("veh-3", at(9, 40, 5))])).values())
    only_prop = detection_latency([TELE], [prop("veh-3", at(9, 40, 5))])
    assert only_prop["truth_to_approval_s"] is None and only_prop["approval_to_pull_s"] is None


def test_only_the_first_pull_and_a_proposal_after_the_truth_count():
    early = prop("veh-3", at(8), seq=0)                                                     # an earlier, unrelated run
    comp = pull("veh-3", at(9, 50), seq=3, compensation=True)
    later = pull("veh-3", at(9, 59), seq=4)
    other = AuditEvent(seq=5, kind="write", run_id="r1", data={"action": "create_work_order", "at": iso(at(9, 40, 30)),
                                                              "vehicle": "veh-3"})
    got = detection_latency([TELE], [early, other, comp, later, pull("veh-3", at(9, 41)), prop("veh-3", at(9, 40, 5))])
    assert got["truth_to_first_proposal_s"] == 5.0 and got["truth_to_pull_s"] == 60.0


def test_sub_second_lag_is_kept():
    got = detection_latency([obs("veh-3", at(9, 40), lag=0.7)], [prop("veh-3", at(9, 40) + timedelta(seconds=0.7))])
    assert got["truth_to_telematics_s"] == 0.7 and got["truth_to_first_proposal_s"] == 0.7


def test_two_vehicles_each_get_their_own_run_reviewer_repro():
    """veh-3 faults at 08:00 and is never planned; veh-5 faults at 09:00 and is planned at 09:00:05. The old code
    paired veh-5's proposal with veh-3's truth and reported 3605.0."""
    log = [obs("veh-3", at(8)), obs("veh-5", at(9))]
    audit = [prop("veh-5", at(9, 0, 5)), appr("veh-5", at(9, 1)), pull("veh-5", at(9, 1))]
    got = detection_latency(log, audit)
    assert got["truth_to_first_proposal_s"] is None and got["truth_to_pull_s"] is None     # scenario level = earliest fault
    assert got["per_vehicle"]["veh-3"]["truth_to_first_proposal_s"] is None
    v5 = got["per_vehicle"]["veh-5"]
    assert (v5["truth_to_telematics_s"], v5["truth_to_first_proposal_s"], v5["truth_to_pull_s"]) == (5.0, 5.0, 60.0)


def test_truth_comes_from_ground_truth_beats_when_given_even_if_no_product_saw_it():
    beats = [Beat(at(9, 40), "veh-3", "fault_on", {"spn": 110, "fmi": 0}), Beat(at(10, 15), "veh-3", "fault_off", {})]
    got = detection_latency([], [prop("veh-3", at(9, 40, 5))], beats)
    assert got["truth_to_telematics_s"] is None and got["truth_to_first_proposal_s"] == 5.0


def test_uses_the_earliest_truth_when_several_faults():
    got = detection_latency([obs("veh-1", at(14)), TELE], [prop("veh-3", at(9, 40, 5))])
    assert got["truth_to_telematics_s"] == 5.0 and got["truth_to_first_proposal_s"] == 5.0
