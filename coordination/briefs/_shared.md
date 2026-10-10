# Shared rules for every track lead
- You work in your own git worktree/branch. Edit ONLY your directories (named in your brief) and your tests. Never edit `src/transitos/contracts/`, `docs/MOCK_API.md`, or other tracks' code.
- Read first: CLAUDE.md, docs/CONTRACTS.md, docs/MOCK_API.md, docs/ASSUMPTIONS.md, then `src/transitos/contracts/*.py`, then your brief.
- Local profile only: SQLite, in-process, no Docker, no network except localhost TestClient. No real LLM calls. Python 3.11, `uv run pytest`. Keep dependencies to those in pyproject.toml unless essential (if essential, note it in your handoff; do not edit pyproject).
- If you need a contract change: write `coordination/requests/<track>-<n>.md` and work around it with a local shim meanwhile.
- Write tests first for hard rules. Done = `uv run pytest -q` output pasted in your handoff, plus `ruff check` clean on your dirs.
- Final deliverable: commit on your branch and write `coordination/handoffs/<track>.md`: what you built, how to run, public entry points (module paths + signatures other tracks use), assumptions made (add new ones with IDs A11+ in the handoff, not in docs), anything you need from others, what is NOT done. Be honest about gaps.
- Reply with a <=15-line summary.
