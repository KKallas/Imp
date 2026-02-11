#!/usr/bin/env python3
"""
Shared state tracking for 99-tools agents.

Tracks token usage across runs so you can set a budget and resume later.

Usage as CLI:
    python _state.py status    # Show token usage and run history
    python _state.py reset     # Reset counters (e.g. after buying more tokens)

Usage as module:
    from _state import record_run, check_budget, get_tokens_used
"""

import json
import sys
from datetime import datetime
from pathlib import Path

STATE_FILE = Path(__file__).parent / ".state.json"


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"total_input_tokens": 0, "total_output_tokens": 0, "total_cost_usd": 0.0, "runs": []}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def get_tokens_used() -> int:
    state = load_state()
    return state.get("total_input_tokens", 0) + state.get("total_output_tokens", 0)


def check_budget(max_tokens: int) -> bool:
    """Returns True if there's budget remaining."""
    return get_tokens_used() < max_tokens


def record_run(script: str, target: str, input_tokens: int = 0, output_tokens: int = 0,
               cost_usd: float = 0.0, success: bool = True) -> dict:
    """Record a Claude run and return updated state."""
    state = load_state()
    state["total_input_tokens"] = state.get("total_input_tokens", 0) + input_tokens
    state["total_output_tokens"] = state.get("total_output_tokens", 0) + output_tokens
    state["total_cost_usd"] = round(state.get("total_cost_usd", 0) + cost_usd, 4)
    state["runs"].append({
        "timestamp": datetime.now().isoformat(),
        "script": script,
        "target": target,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": cost_usd,
        "success": success
    })
    save_state(state)
    return state


def get_run_count() -> int:
    """Return the total number of recorded runs."""
    state = load_state()
    return len(state.get("runs", []))


def reset_state():
    save_state({"total_input_tokens": 0, "total_output_tokens": 0, "total_cost_usd": 0.0, "runs": []})
    print("State reset.")


def print_status():
    state = load_state()
    total = state.get("total_input_tokens", 0) + state.get("total_output_tokens", 0)
    runs = state.get("runs", [])
    cost = state.get("total_cost_usd", 0)

    print(f"Tokens used:  {total:,} ({state.get('total_input_tokens', 0):,} in / {state.get('total_output_tokens', 0):,} out)")
    print(f"Cost:         ${cost:.4f}")
    print(f"Total runs:   {len(runs)}")

    if runs:
        print(f"\nRecent runs:")
        for r in runs[-10:]:
            status = "OK" if r.get("success") else "FAIL"
            tokens = r.get("input_tokens", 0) + r.get("output_tokens", 0)
            print(f"  [{status}] {r['script']} {r['target']} - {tokens:,} tokens (${r.get('cost_usd', 0):.4f}) - {r['timestamp']}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "reset":
        reset_state()
    elif cmd == "status":
        print_status()
    elif cmd == "run-count":
        print(get_run_count())
    else:
        print(f"Usage: python _state.py [status|reset|run-count]")
