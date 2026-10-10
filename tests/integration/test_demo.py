from transitos.integration import demo


def test_demo_runs_happy_path_to_done(capsys):
    assert demo.main() == 0
    out = capsys.readouterr().out
    for stage in ("[1] Fault raised", "[3] Plan proposed", "[5] Five writes", "[7] Return-to-service",
                  "[8] Audit trail", "Invariants (Track F): all hold"):
        assert stage in out
    assert out.count("idempotency key") == 5 and "-> DONE" in out
