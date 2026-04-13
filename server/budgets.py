"""Budget tracking for Imp — three independent counters in `.imp/state.json`.

Imp tracks three budgets (see v0.1.md §Budgets):

  - **tokens**  — Claude API tokens (in + out) across every agent and
                  pipeline invocation. Default 200,000. Cost control.
  - **edits**   — Approved checkpoint-B writes to GitHub. Default 50.
                  Mutation rate-limiting.
  - **tasks**   — Pipeline-script invocations (moderate_issues.py,
                  solve_issues.py, fix_prs.py, run_all.sh). Default 10.
                  Coarse "how much work" knob.

When any budget hits zero, `server/intercept.py` rejects the **next** write
action at checkpoint B. **In-flight subprocesses are never killed by a budget
tick** — they finish on their own cap; the budget exhausts "into" the task,
not "through" it.

This module has zero dependencies on chainlit so it can be unit-tested in
isolation. The legacy `99-tools/_state.py` is a thin shim over these
functions so the CLI mode (`./99-tools/run_all.sh`) reads and writes the
same counters as the chat mode.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / ".imp" / "state.json"

COUNTERS = ("tokens", "edits", "tasks")

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

    def any_exhausted(self) -> bool:
        return any(self.exhausted(c) for c in COUNTERS)

    def to_dict(self) -> dict:
        return {
            "tokens": {
                "used": self.tokens_used,
                "limit": self.tokens_limit,
                "remaining": self.remaining("tokens"),
            },
            "edits": {
                "used": self.edits_used,
                "limit": self.edits_limit,
                "remaining": self.remaining("edits"),
            },
            "tasks": {
                "used": self.tasks_used,
                "limit": self.tasks_limit,
                "remaining": self.remaining("tasks"),
            },
        }


# ---------- low-level file I/O ----------


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


def _validate_counter(counter: str) -> None:
    if counter not in COUNTERS:
        raise ValueError(f"unknown counter {counter!r}; expected one of {COUNTERS}")


# ---------- read ----------


def get_budgets() -> BudgetState:
    """Return the three counters + limits as a `BudgetState`.

    `BudgetState.to_dict()` gives the `{tokens: {used, limit, remaining}, ...}`
    shape used by chat tools and the sidebar.
    """
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


# ---------- limit setters (chat tools) ----------


def set_limit(counter: str, value: int) -> None:
    _validate_counter(counter)
    if value < 0:
        raise ValueError(f"limit must be >= 0, got {value}")
    state = _load_state()
    state.setdefault("limits", dict(DEFAULT_LIMITS))[counter] = int(value)
    _save_state(state)


def set_token_budget(n: int) -> None:
    """Chat tool: set the token budget limit."""
    set_limit("tokens", n)


def set_edit_budget(n: int) -> None:
    """Chat tool: set the edit budget limit."""
    set_limit("edits", n)


def set_task_budget(n: int) -> None:
    """Chat tool: set the task budget limit."""
    set_limit("tasks", n)


# ---------- counter resets (chat tools) ----------


def reset_counter(counter: str) -> None:
    _validate_counter(counter)
    state = _load_state()
    state.setdefault("counters", {})[counter] = 0
    _save_state(state)


def reset_all_counters() -> None:
    state = _load_state()
    state["counters"] = {c: 0 for c in COUNTERS}
    _save_state(state)


def reset_budgets(which: Optional[Iterable[str]] = None) -> None:
    """Chat tool: reset one or more counters.

    `which=None` resets all three. `which=["tokens"]` resets just tokens.
    """
    if which is None:
        reset_all_counters()
        return
    targets = list(which)
    for counter in targets:
        _validate_counter(counter)
    for counter in targets:
        reset_counter(counter)


# ---------- counter increments (accounting) ----------


def add_tokens(input_tokens: int, output_tokens: int) -> None:
    """Fed by the Claude SDK token-usage callback and the legacy shim."""
    if input_tokens < 0 or output_tokens < 0:
        raise ValueError(
            f"token deltas must be >= 0, got in={input_tokens} out={output_tokens}"
        )
    state = _load_state()
    counters = state.setdefault("counters", {})
    counters["tokens"] = counters.get("tokens", 0) + input_tokens + output_tokens
    _save_state(state)


def increment_edits(n: int = 1) -> None:
    """Called by intercept.py after a successful checkpoint-B write."""
    state = _load_state()
    counters = state.setdefault("counters", {})
    counters["edits"] = counters.get("edits", 0) + n
    _save_state(state)


def increment_tasks(n: int = 1) -> None:
    """Called by intercept.py after a successful pipeline-script run."""
    state = _load_state()
    counters = state.setdefault("counters", {})
    counters["tasks"] = counters.get("tasks", 0) + n
    _save_state(state)


# ---------- per-invocation cap for pipeline scripts ----------


def per_invocation_token_cap(cap_default: int = PER_INVOCATION_CAP_DEFAULT) -> int:
    """Return the `--max-tokens N` value the worker should pass to a new
    pipeline script. `N = min(remaining_tokens, cap_default)`, so a single
    run can never swallow the whole remaining budget.

    Callers should check `remaining >= PER_INVOCATION_CAP_FLOOR` before
    starting a run; if not, intercept.py rejects the action outright.
    """
    remaining = get_budgets().remaining("tokens")
    return max(0, min(remaining, cap_default))
