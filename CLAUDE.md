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
7. LLM access only through `workflow/reasoning.py` client interface; default is a stub plus code-ranked fallback (provider decision is open).
8. Local profile = zero infra (SQLite + in-process bus). Kafka/Postgres compose profile is later; keep interfaces identical.
9. Keep client names and client documents out of this repo.

