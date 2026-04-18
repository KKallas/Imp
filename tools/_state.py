#!/usr/bin/env python3
"""Legacy state shim — forwards to server/budgets.py.

Historically this module kept its own `.state.json` in `99-tools/` with a
`total_input_tokens / total_output_tokens / runs` schema. Under the v0.1
design all state lives in `.imp/state.json` via `server/budgets.py`, so
chat-mode and CLI-mode share the same three counters (tokens, edits, tasks).

This file keeps the original public API (`get_tokens_used`, `check_budget`,
`record_run`, `reset_state`, `print_status`, `get_run_count`,
`load_state`, `save_state`) so `moderate_issues.py`, `solve_issues.py`,
`fix_prs.py`, and `run_all.sh` don't need to change. Token deltas passed
to `record_run` are added to the shared token counter; a successful run
increments nothing else here — the tasks counter is bumped by
`server/intercept.py` after the subprocess exits (the authoritative
"invocation count" boundary). Cost tracking (`total_cost_usd`) and the
detailed per-run log are dropped — the `.imp/output/<action_id>.log`
files plus token counters are the new source of truth.

A full rewrite lives behind KKallas/Imp#20 (P5.20); this shim exists so
P2.8 can ship a single state file without rewriting the CLI pipeline.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import budgets  # noqa: E402

# Preserved for backwards-compat. Nothing writes here anymore; the legacy
# `.state.json` is effectively dead.
STATE_FILE = Path(__file__).parent / ".state.json"


def load_state() -> dict:
    """Return a dict in the legacy shape, sourced from `.imp/state.json`.

    `total_input_tokens` and `total_output_tokens` can't be split anymore
    (the shared counter sums them), so the whole token count lands in
    `total_input_tokens` and `total_output_tokens` stays 0.
    """
    b = budgets.get_budgets()
    return {
        "total_input_tokens": b.tokens_used,
        "total_output_tokens": 0,
        "total_cost_usd": 0.0,
        "runs": [],
    }


def save_state(state: dict) -> None:
    """No-op — state is owned by server/budgets.py now.

    Kept so any lingering callers don't crash. Token deltas should go
    through `record_run` / `budgets.add_tokens`, limits through
    `budgets.set_*_budget`.
    """
    return None


def get_tokens_used() -> int:
    return budgets.get_budgets().tokens_used


def check_budget(max_tokens: int) -> bool:
    """Returns True if there's budget remaining under `max_tokens`.

    Also returns False if the shared token counter is already exhausted,
    regardless of the caller's `max_tokens` argument — chat mode may have
    set a tighter cap that the CLI pipeline should honour.
    """
    b = budgets.get_budgets()
    if b.exhausted("tokens"):
        return False
    return b.tokens_used < max_tokens


def record_run(
    script: str,
    target: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: float = 0.0,
    success: bool = True,
) -> dict:
    """Account tokens from a single Claude call inside a pipeline script.

    The per-run detail fields (`script`, `target`, `cost_usd`, `success`)
    are accepted but not persisted — they're now captured in the action
    log + per-action `.imp/output/*.log` files that intercept.py writes.
    """
    budgets.add_tokens(input_tokens, output_tokens)
    return load_state()


def get_run_count() -> int:
    """Legacy "how many runs this session" — mapped to the tasks counter."""
    return budgets.get_budgets().tasks_used


def reset_state() -> None:
    budgets.reset_all_counters()
    print("State reset.")


def print_status() -> None:
    b = budgets.get_budgets()
    print(
        f"Tokens used:  {b.tokens_used:,} / {b.tokens_limit:,} "
        f"({b.remaining('tokens'):,} remaining)"
    )
    print(
        f"Edits used:   {b.edits_used} / {b.edits_limit} "
        f"({b.remaining('edits')} remaining)"
    )
    print(
        f"Tasks used:   {b.tasks_used} / {b.tasks_limit} "
        f"({b.remaining('tasks')} remaining)"
    )


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "reset":
        reset_state()
    elif cmd == "status":
        print_status()
    elif cmd == "run-count":
        print(get_run_count())
    else:
        print("Usage: python _state.py [status|reset|run-count]")
