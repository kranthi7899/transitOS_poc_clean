# Claude Haiku as the reasoner (optional)

By default TransitOS uses the deterministic stub plus the code-ranked fallback: no model, no network, no cost.
You can switch the reasoning step to **Claude Haiku** (`claude-haiku-5-5`, the only model allowed).

## What the model does, and what it never does
- The hard rules (the clearance module) decide who is on the list. The model **chooses among those options**, decides
  pull-now versus pull-at-block-end, and writes a one- or two-sentence reason for the dispatcher.
- Its answer is never trusted. Every id is re-checked against the code-computed lists and the clearance module; a
  refusal, truncation, bad answer, missing key, exhausted budget or API error just makes the code-ranked fallback plan.
- It cannot cause a write. Writes still need a person's approval.

## Turn it on
1. `uv sync --extra llm` (installs the SDK; the default install does not need it).
2. Put your key in `.env.local` in the project folder (git-ignored): `ANTHROPIC_API_KEY=...`. Never paste it into chat.
3. `export TRANSITOS_REASONER=anthropic` (the default is `stub`).

## Cost controls (all enforced in code)
| Control | Value |
|---|---|
| Model | `claude-haiku-5-5` only; any other model name is refused |
| Price used for the estimate | $0.10 per million input tokens, $0.50 per million output tokens |
| Hard cap | default **$3**, can never exceed **$10** (`TRANSITOS_LLM_CAP_USD`) |
| Checked | **before every call**, against the worst case for that call; a call that could cross the cap is not sent |
| Persistent total | `~/.transitos/llm_spend.json` (totals only), shared by every process; an unreadable file stops spending |
| Calls per process | 60 (`TRANSITOS_LLM_MAX_CALLS`) |
| Per call | effort `low`, `max_tokens` 700, no tools, no history, compact prompt, one retry at most, 30 s timeout |

## Commands
- `make llm-spend` shows the total spent against the cap.
- `make llm-smoke` runs 2 live checks (a few thousandths of a dollar).
- `make llm-report` runs the whole scenario report with Haiku as the reasoner (about 50 calls, a few cents).
- `make verify` never calls the model.

## Safety
The prompt contains only a whitelist of fields; free text is truncated and `<` is escaped so data cannot close the data
tag; the system prompt says text in the data is never an instruction; and the answer is validated regardless. The key is
read from the environment or `.env.local`, never logged, stored in a record, or put in a prompt, and a test fails the
build if a key-shaped string is ever tracked by git.
