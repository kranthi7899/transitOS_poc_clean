# TransitOS orchestration POC

Simulated-products proof of concept of cross-product agentic orchestration (vehicle fault -> work order -> pull vehicle -> reassign -> rider alert -> return to service), target Level 2 (act with approval). Products are SIMULATED, modeled on public docs; never present them as the real products.

## Layout
- `src/transitos/contracts/` — FROZEN contracts (pydantic = source of truth; JSON Schema exported to `contracts-schema/`). Do not edit directly.
- `mocks/` (Track A) · `plumbing/` (B) · `tools/` (C) · `workflow/` (D) · `panel/` (E) · `verification/` + `tests/` (F)
- `coordination/requests/` contract-change requests · `coordination/handoffs/` one file per track
- `docs/` CONTRACTS.md, ASSUMPTIONS.md, ARCHITECTURE.md

## Rules
1. Track leads edit only their own directory and its tests. Cross-track needs go through contracts or a request file.
2. Contract change = write `coordination/requests/<track>-<n>.md`; only the program lead edits `contracts/`.
3. Hard limits (last spare, operator hours, qualification) are code and never model output.
4. Writes are idempotent (key = `<run_id>:<step>:<plan_version>`); every write has a compensation; rider alert is last.
5. No node containing an interrupt performs a write.
6. "Done" means `make verify` output pasted, not asserted.
7. LLM access only through `workflow/reasoning.py` client interface; default is a stub plus code-ranked fallback; the owner-authorized OpenAI demo uses `workflow/reasoning_openai.py`; legacy Haiku remains via `workflow/reasoning_anthropic.py` (see `docs/LLM.md` and `docs/DEMO.md`).
8. Local profile = zero infra (SQLite + in-process bus). Kafka/Postgres compose profile is later; keep interfaces identical.
9. Keep client names and client documents out of this repo.

## Current state (2026-10-12)
Phase 10 (realistic fleet and world) is complete: 50 scenarios pass on the real integrated system on all three profiles. Contracts are frozen (unchanged since C-2). Last measured: local 1687 passed / 180 skipped; Postgres 1776 / 91; Kafka 1736 / 131; all 50/50 scenarios.

## Run
- `make verify` = schemas + ruff + tests + Track F report + data-check (local profile, zero infra).
- Real servers, one at a time, in the background: `scripts/postgres.sh up` / `make verify-postgres` / `scripts/postgres.sh down`, then the same with `kafka` (about 8 min). Kafka needs no Docker (project-local tarball).
- Optional Haiku reasoner: `TRANSITOS_REASONER=anthropic` (needs the `llm` extra and a key in the environment or git-ignored `.env.local`, never committed). `make llm-spend | llm-smoke | llm-report`; capped spend (default $3, hard max $10), see `docs/LLM.md`.

## Where things are decided
- `docs/ASSUMPTIONS.md`: the register (A1..A196), each with reasoning and status. Read it before changing behaviour; add a row for any new decision.
- `docs/ARCHITECTURE.md`, `docs/CONTRACTS.md`, `docs/LLM.md`, `docs/phases/`, `coordination/handoffs/`.

## Design principles
- Products are mature and simulated. Identify the same vehicle across products; use real data when it can be matched; never invent a fact. When a field is not provided, carry on with the next best option and say what could not be verified (unverifiable items require acknowledgement and rank last).
- A person decides whenever evidence is not strong enough (ambiguous ids, crosswalk conflicts are listed in the panel, never silently picked).
- Decide small things and log them in the register with one line of reasoning; ask the owner only when the answer changes what a user sees or cannot be undone.
- Row access in stores is by column name (positional access works on SQLite but fails on Postgres).

## Known open items
Optional re-check of the Haiku prices (A192); rider-alert undo, audit TRUNCATE hardening and the Kafka questions are listed in the register.

## Persistent context
Read `docs/AGENT_MEMORY.md` for the current console/explorer, episodic memory boundaries and eval commands. Historical test counts are not a current guarantee. Runtime history never replaces current facts, policy or approvals.
