"""OpenAI proposal-only reasoner, with a durable $0.50 demo spending ceiling (A197).

Uses Responses JSON Schema output; workflow.reasoning.validate_output remains authoritative.
No hidden HTTP retries. Reserve before sending; uncertain failures keep their reservation.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path

import httpx

from transitos.workflow.reasoning_anthropic import SCHEMA, SYSTEM, build_prompt

MODEL = "gpt-4o-mini"
MAX_OUTPUT_TOKENS = 1000
CAP_USD = 0.50
# Verified against https://developers.openai.com/api/docs/models/gpt-4o-mini on 2026-10-12.
INPUT_PRICE = 0.15 / 1_000_000
OUTPUT_PRICE = 0.60 / 1_000_000
PROJECT_ROOT = Path(__file__).resolve().parents[3]


class OpenAIUnavailable(Exception):
    """Safe, secret-free failure; the graph uses its validated code-ranked fallback."""


class OpenAIConfigError(ValueError):
    pass


def load_openai_key() -> None:
    """Load only the key; an existing environment variable always wins."""
    if "OPENAI_API_KEY" in os.environ:
        return
    for path in (Path(".env.local"), PROJECT_ROOT / ".env.local"):
        if not path.is_file():
            continue
        for line in path.read_text().splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and key.strip() == "OPENAI_API_KEY" and value.strip():
                os.environ["OPENAI_API_KEY"] = value.strip().strip("'\"")
                return


class DemoBudget:
    """Atomic cross-process reservations in SQLite; reset of the demo never clears this ledger."""

    def __init__(self, path: str | Path | None = None, cap: float = CAP_USD):
        if not math.isfinite(cap) or cap < 0:
            raise OpenAIConfigError("OpenAI spend cap must be a finite nonnegative number")
        self.path = Path(path or PROJECT_ROOT / ".local" / "openai-spend.sqlite")
        self.cap = min(cap, CAP_USD)

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        c.row_factory = sqlite3.Row
        try:
            c.execute("CREATE TABLE IF NOT EXISTS calls (id INTEGER PRIMARY KEY, cost REAL NOT NULL, settled INTEGER NOT NULL)")
        except sqlite3.Error:
            c.close()
            raise
        return c

    def status(self) -> dict:
        try:
            with closing(self._connect()) as c:
                r = c.execute("SELECT COALESCE(SUM(cost),0) AS total, COUNT(*) AS calls FROM calls").fetchone()
                return {"spent_usd": r["total"], "cap_usd": self.cap, "calls": r["calls"], "available": True}
        except (sqlite3.Error, OSError):
            return {"spent_usd": None, "cap_usd": self.cap, "calls": None, "available": False}

    def reserve(self, worst: float) -> int:
        if not math.isfinite(worst) or worst < 0:
            raise OpenAIUnavailable("Invalid request cost estimate")
        try:
            c = self._connect()
            try:
                c.execute("BEGIN IMMEDIATE")
                total = c.execute("SELECT COALESCE(SUM(cost),0) AS total FROM calls").fetchone()["total"]
                if total + worst > self.cap:
                    raise OpenAIUnavailable("Demo spend cap reached; no request sent")
                token = c.execute("INSERT INTO calls(cost,settled) VALUES (?,0)", (worst,)).lastrowid
                c.execute("COMMIT")
                return token
            finally:
                c.close()  # an outstanding transaction rolls back on close
        except (sqlite3.Error, OSError) as e:
            raise OpenAIUnavailable("Spend ledger unavailable; no request sent") from e

    def settle(self, token: int, cost: float) -> None:
        if not math.isfinite(cost) or cost < 0:
            return  # keep the conservative reservation
        try:
            with closing(self._connect()) as c:
                c.execute("UPDATE calls SET cost=?, settled=1 WHERE id=? AND settled=0", (cost, token))
        except (sqlite3.Error, OSError) as e:
            raise OpenAIUnavailable("Could not record usage; reserved cost retained") from e


class OpenAIReasoner:
    name = MODEL
    prompt_version = "openai-demo-v1"

    def __init__(self, *, key: str | None = None, client: httpx.Client | None = None,
                 budget: DemoBudget | None = None):
        self._key = key
        self._client = client
        self.budget = budget or DemoBudget()

    def propose(self, facts: dict, attempt: int = 0) -> dict:
        payload = {"model": MODEL, "instructions": SYSTEM + (
            " Prior incidents are historical context only, never current availability, policy or permission. "
            "Choose only from today's supplied feasible options; every new run needs its own approval."), "input": build_prompt(facts),
                   "max_output_tokens": MAX_OUTPUT_TOKENS, "store": False,
                   "text": {"format": {"type": "json_schema", "name": "dispatcher_proposal",
                                       "strict": True, "schema": SCHEMA}}}
        # UTF-8 bytes bound tokenized text conservatively; extra room for schema/framing.
        input_bound = len(json.dumps(payload, ensure_ascii=False).encode()) + 4096
        if input_bound > 24000:
            raise OpenAIUnavailable("Proposal context exceeds the demo request bound")
        token = self.budget.reserve(input_bound * INPUT_PRICE + MAX_OUTPUT_TOKENS * OUTPUT_PRICE)
        own_client = self._client is None
        client = self._client or httpx.Client(timeout=20.0, follow_redirects=False)
        try:
            response = client.post("https://api.openai.com/v1/responses", json=payload,
                                   headers={"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"})
            if response.status_code != 200:
                # Never include the vendor response/body or a key-bearing request in an error.
                raise OpenAIUnavailable(f"OpenAI request failed (HTTP {response.status_code}); reserved cost retained")
            doc = response.json()
            usage = doc.get("usage") or {}
            it, ot = usage.get("input_tokens"), usage.get("output_tokens")
            if isinstance(it, int) and isinstance(ot, int) and it >= 0 and ot >= 0:
                self.budget.settle(token, it * INPUT_PRICE + ot * OUTPUT_PRICE)
            if doc.get("status") != "completed":
                raise OpenAIUnavailable("OpenAI answer incomplete")
            content = [part for message in doc.get("output", []) for part in message.get("content", [])]
            if any(part.get("type") == "refusal" for part in content):
                raise OpenAIUnavailable("OpenAI declined the proposal")
            value = "".join(part.get("text", "") for part in content if part.get("type") == "output_text")
            raw = json.loads(value)
            if not isinstance(raw, dict):
                raise OpenAIUnavailable("OpenAI proposal was not an object")
            raw["reasoning"] = str(raw.get("reasoning", ""))[:300]
            raw.setdefault("alternatives", [])
            return raw
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            raise OpenAIUnavailable("OpenAI response unavailable or malformed; reserved cost retained") from e
        finally:
            if own_client:
                client.close()


def reasoner_from_env(env: Mapping[str, str] | None = None, *, client=None, load_env_file: bool = True, **kw):
    env = os.environ if env is None else env
    if env is os.environ and load_env_file:
        load_openai_key()
    key = env.get("OPENAI_API_KEY", "").strip()
    if not key and client is None:
        raise OpenAIConfigError("OpenAI mode needs OPENAI_API_KEY in the environment or .env.local")
    budget = kw.pop("budget", None) or DemoBudget(
        env.get("TRANSITOS_OPENAI_SPEND_FILE"), float(env.get("TRANSITOS_LLM_CAP_USD", CAP_USD)))
    return OpenAIReasoner(key=key, client=client, budget=budget, **kw)


def main() -> None:
    budget = DemoBudget(os.environ.get("TRANSITOS_OPENAI_SPEND_FILE"),
                        float(os.environ.get("TRANSITOS_LLM_CAP_USD", CAP_USD)))
    print(json.dumps(budget.status(), indent=2))


if __name__ == "__main__":
    main()
