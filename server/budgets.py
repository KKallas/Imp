"""Budget tracking for Imp — minimal stub.

The full acceptance criteria for this module live in KKallas/Imp#8. This
file implements just enough to give server/intercept.py something to call:
atomic counter reads/writes backed by `.imp/state.json`, default limits,
and helpers the interception layer needs (exhausted / remaining / floor).

What's still missing vs. the full issue:
  - Sophisticated "budget exhaustion while a subprocess is running" logic
    (documented in v0.1.md but not yet enforced beyond the "reject next
    action if exhausted" path here)
  - 99-tools/_state.py shim (that's KKallas/Imp#20)
  - Chat tools for set_token_budget / reset_budgets (those go in
    foreman_agent.py / setup_agent.py later)
  - Token-usage integration with the claude-agent-sdk callback

This module intentionally has zero dependencies on chainlit so it can be
unit-tested in isolation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / ".imp" / "state.json"

DEFAULT_LIMITS = {"tokens": 200_000, "edits": 50, "tasks": 10}

# Default max tokens passed as --max-tokens to a single pipeline invocation.
# The worker computes min(remaining_tokens, PER_INVOCATION_CAP_DEFAULT) when
# proposing a solve_issues.py / moderate_issues.py / fix_prs.py run.
PER_INVOCATION_CAP_DEFAULT = 25_000

# Floor below which the interception layer refuses to start a new pipeline
# invocation: below this many remaining tokens, the guard rejects with
# "not enough budget to start a new task" instead of approving a run that
# would immediately fail for lack of budget.
PER_INVOCATION_CAP_FLOOR = 2_000


@dataclass
class BudgetState:
    tokens_used: int
    tokens_limit: int
    edits_used: int
    edits_limit: int
    tasks_used: int
    tasks_limit: int

    def remaining(self, counter: str) -> int:
        return max(
            0,
            getattr(self, f"{counter}_limit") - getattr(self, f"{counter}_used"),
        )

    def exhausted(self, counter: str) -> bool:
        return self.remaining(counter) <= 0

    def to_dict(self) -> dict:
        return {
            "tokens": {"used": self.tokens_used, "limit": self.tokens_limit},
            "edits": {"used": self.edits_used, "limit": self.edits_limit},
            "tasks": {"used": self.tasks_used, "limit": self.tasks_limit},
        }


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def get_budgets() -> BudgetState:
    state = _load_state()
    counters = state.get("counters", {})
    limits = state.get("limits", DEFAULT_LIMITS)
    return BudgetState(
        tokens_used=counters.get("tokens", 0),
        tokens_limit=limits.get("tokens", DEFAULT_LIMITS["tokens"]),
        edits_used=counters.get("edits", 0),
        edits_limit=limits.get("edits", DEFAULT_LIMITS["edits"]),
        tasks_used=counters.get("tasks", 0),
        tasks_limit=limits.get("tasks", DEFAULT_LIMITS["tasks"]),
    )


def set_limit(counter: str, value: int) -> None:
    assert counter in ("tokens", "edits", "tasks"), counter
    assert value >= 0, value
    state = _load_state()
    state.setdefault("limits", dict(DEFAULT_LIMITS))[counter] = value
    _save_state(state)


def reset_counter(counter: str) -> None:
    assert counter in ("tokens", "edits", "tasks"), counter
    state = _load_state()
    state.setdefault("counters", {})[counter] = 0
    _save_state(state)


def reset_all_counters() -> None:
    state = _load_state()
    state["counters"] = {"tokens": 0, "edits": 0, "tasks": 0}
    _save_state(state)


def add_tokens(input_tokens: int, output_tokens: int) -> None:
    state = _load_state()
    counters = state.setdefault("counters", {})
    counters["tokens"] = counters.get("tokens", 0) + input_tokens + output_tokens
    _save_state(state)


def increment_edits(n: int = 1) -> None:
    state = _load_state()
    counters = state.setdefault("counters", {})
    counters["edits"] = counters.get("edits", 0) + n
    _save_state(state)


def increment_tasks(n: int = 1) -> None:
    state = _load_state()
    counters = state.setdefault("counters", {})
    counters["tasks"] = counters.get("tasks", 0) + n
    _save_state(state)
