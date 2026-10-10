import json

from transitos.verification import report
from transitos.verification.fake_world import FakeWorld
from transitos.verification.runner import classify, run_scenario
from transitos.verification.scenarios import BY_ID, SCENARIOS, Outcome


def test_at_least_twenty_scenarios_with_unique_ids_and_all_outcome_kinds():
    assert len(SCENARIOS) >= 20 and len({s.id for s in SCENARIOS}) == len(SCENARIOS)
    assert {s.expected for s in SCENARIOS} == set(Outcome)


def test_required_scenario_topics_present():
    for sid in ["severe_fault_with_spare", "pm_overdue_urgent", "dvir_defect", "last_spare", "wheelchair_mismatch",
                "operator_hours_exhausted", "unqualified_operator", "no_feasible_spare", "duplicate_trigger",
                "trigger_vehicle_active_run", "human_wo_exists", "unresolved_vehicle_id", "product_down_on_read"]:
        assert sid in BY_ID


def test_report_table_and_json(tmp_path):
    results = report.run_all(FakeWorld)
    assert all(r.passed for r in results)
    table = report.format_table(results)
    assert "22/22" in table or f"{len(results)}/{len(results)}" in table
    out = tmp_path / "r.json"
    assert report.main(["--out", str(out)]) == 0
    data = json.loads(out.read_text())
    assert data["total"] == len(SCENARIOS) and data["passed"] == data["total"]


def test_report_flags_a_broken_world():
    class Broken(FakeWorld):
        def approve(self, run_id, scope="plan", by="dispatcher"):
            self.audit = []          # a system that loses its approvals
            super().approve(run_id, scope, by)
            self.audit = [a for a in self.audit if a.kind != "approval"]

    res = run_scenario(Broken(), BY_ID["severe_fault_with_spare"])
    assert not res.passed and res.invariants["approval_before_write"]
    assert report.format_table([res]).count("FAIL") >= 2


def test_crashing_world_is_reported_not_raised():
    class Crash(FakeWorld):
        def deliver(self, event):
            raise RuntimeError("boom")

    rs = report.run_all(Crash, (BY_ID["last_spare"],))
    assert rs[0].outcome == "ERROR" and "boom" in rs[0].mismatches[0]


def test_classify_empty():
    assert classify(FakeWorld().observe()) == "no-run"
