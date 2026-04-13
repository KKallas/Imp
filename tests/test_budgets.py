"""Tests for server/budgets.py and the 99-tools/_state.py shim.

Run directly: `.venv/bin/python tests/test_budgets.py`
No pytest. Asserts → exit 0 on success, exit 1 on failure.

STATE_FILE is redirected to a tempfile at import time so tests don't
clobber the real `.imp/state.json`.
"""

from __future__ import annotations

import importlib
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "99-tools"))

from server import budgets  # noqa: E402

# Redirect state file so tests don't touch the real .imp/state.json
_TMP_DIR = Path(tempfile.mkdtemp(prefix="imp-budget-test-"))
budgets.STATE_FILE = _TMP_DIR / "state.json"

# Import the legacy shim AFTER redirecting STATE_FILE — the shim imports
# `server.budgets`, so its reads/writes will go through the patched path.
import _state as legacy_state  # noqa: E402

# Force-reimport in case a prior test run left `_state` cached against a
# stale STATE_FILE.
legacy_state = importlib.reload(legacy_state)


def _reset() -> None:
    """Clear state between tests — use the real reset path so we also
    exercise it."""
    if budgets.STATE_FILE.exists():
        budgets.STATE_FILE.unlink()


# ---------- get_budgets shape ----------


def test_get_budgets_defaults_on_empty_state() -> None:
    _reset()
    b = budgets.get_budgets()
    assert b.tokens_used == 0
    assert b.tokens_limit == budgets.DEFAULT_LIMITS["tokens"]
    assert b.edits_used == 0
    assert b.edits_limit == budgets.DEFAULT_LIMITS["edits"]
    assert b.tasks_used == 0
    assert b.tasks_limit == budgets.DEFAULT_LIMITS["tasks"]
    print("test_get_budgets_defaults_on_empty_state: OK")


def test_get_budgets_to_dict_shape() -> None:
    _reset()
    d = budgets.get_budgets().to_dict()
    for key in ("tokens", "edits", "tasks"):
        assert key in d, f"missing key {key!r}"
        assert set(d[key].keys()) == {"used", "limit", "remaining"}, d[key]
    # remaining = limit - used when counters are zero
    assert d["tokens"]["remaining"] == budgets.DEFAULT_LIMITS["tokens"]
    print("test_get_budgets_to_dict_shape: OK")


# ---------- setters ----------


def test_set_limits_persist() -> None:
    _reset()
    budgets.set_token_budget(123_456)
    budgets.set_edit_budget(7)
    budgets.set_task_budget(3)

    b = budgets.get_budgets()
    assert b.tokens_limit == 123_456, b.tokens_limit
    assert b.edits_limit == 7, b.edits_limit
    assert b.tasks_limit == 3, b.tasks_limit

    # Check that set_limit dispatches correctly too
    budgets.set_limit("tokens", 999)
    assert budgets.get_budgets().tokens_limit == 999
    print("test_set_limits_persist: OK")


def test_set_limit_rejects_bad_inputs() -> None:
    _reset()
    try:
        budgets.set_limit("not-a-counter", 10)
        assert False, "expected ValueError on unknown counter"
    except ValueError:
        pass
    try:
        budgets.set_token_budget(-1)
        assert False, "expected ValueError on negative limit"
    except ValueError:
        pass
    print("test_set_limit_rejects_bad_inputs: OK")


# ---------- increments ----------


def test_increments_accumulate() -> None:
    _reset()
    budgets.add_tokens(100, 50)
    budgets.add_tokens(25, 25)
    budgets.increment_edits()
    budgets.increment_edits(3)
    budgets.increment_tasks()

    b = budgets.get_budgets()
    assert b.tokens_used == 200, b.tokens_used
    assert b.edits_used == 4, b.edits_used
    assert b.tasks_used == 1, b.tasks_used
    print("test_increments_accumulate: OK")


def test_add_tokens_rejects_negatives() -> None:
    _reset()
    try:
        budgets.add_tokens(-1, 0)
        assert False, "expected ValueError on negative input_tokens"
    except ValueError:
        pass
    try:
        budgets.add_tokens(0, -5)
        assert False, "expected ValueError on negative output_tokens"
    except ValueError:
        pass
    print("test_add_tokens_rejects_negatives: OK")


# ---------- resets ----------


def test_reset_budgets_all() -> None:
    _reset()
    budgets.add_tokens(500, 500)
    budgets.increment_edits(5)
    budgets.increment_tasks(2)
    budgets.reset_budgets()  # no args → reset all three
    b = budgets.get_budgets()
    assert b.tokens_used == 0
    assert b.edits_used == 0
    assert b.tasks_used == 0
    # Limits should be untouched
    assert b.tokens_limit == budgets.DEFAULT_LIMITS["tokens"]
    print("test_reset_budgets_all: OK")


def test_reset_budgets_selective() -> None:
    _reset()
    budgets.add_tokens(1000, 0)
    budgets.increment_edits(5)
    budgets.increment_tasks(2)
    budgets.reset_budgets(which=["tokens", "tasks"])
    b = budgets.get_budgets()
    assert b.tokens_used == 0
    assert b.edits_used == 5, "edits should not have been reset"
    assert b.tasks_used == 0
    print("test_reset_budgets_selective: OK")


def test_reset_budgets_rejects_bad_counter() -> None:
    _reset()
    try:
        budgets.reset_budgets(which=["tokens", "nope"])
        assert False, "expected ValueError on unknown counter"
    except ValueError:
        pass
    # Validation happens before any reset
    budgets.increment_edits(2)
    try:
        budgets.reset_budgets(which=["edits", "bogus"])
        assert False, "expected ValueError on second bad counter"
    except ValueError:
        pass
    assert budgets.get_budgets().edits_used == 2, (
        "edits should not have been partially reset before validation failed"
    )
    print("test_reset_budgets_rejects_bad_counter: OK")


# ---------- exhaustion ----------


def test_exhausted_and_remaining() -> None:
    _reset()
    budgets.set_edit_budget(3)
    budgets.increment_edits(3)
    b = budgets.get_budgets()
    assert b.exhausted("edits")
    assert b.remaining("edits") == 0
    # Over-increment — remaining is clamped at 0, not negative
    budgets.increment_edits(2)
    b2 = budgets.get_budgets()
    assert b2.remaining("edits") == 0
    assert b2.exhausted("edits")
    print("test_exhausted_and_remaining: OK")


def test_any_exhausted() -> None:
    _reset()
    b = budgets.get_budgets()
    assert not b.any_exhausted()
    budgets.set_task_budget(1)
    budgets.increment_tasks(1)
    assert budgets.get_budgets().any_exhausted()
    print("test_any_exhausted: OK")


# ---------- per-invocation cap ----------


def test_per_invocation_cap() -> None:
    _reset()
    budgets.set_token_budget(100_000)
    # When plenty of budget remains, cap == default
    assert budgets.per_invocation_token_cap() == budgets.PER_INVOCATION_CAP_DEFAULT
    # When budget is smaller than default, cap == remaining
    budgets.set_token_budget(5_000)
    assert budgets.per_invocation_token_cap() == 5_000
    # When exhausted, cap is 0
    budgets.add_tokens(5_000, 0)
    assert budgets.per_invocation_token_cap() == 0
    print("test_per_invocation_cap: OK")


# ---------- legacy _state.py shim ----------


def test_shim_reads_through_budgets() -> None:
    _reset()
    budgets.add_tokens(7_000, 3_000)
    assert legacy_state.get_tokens_used() == 10_000, legacy_state.get_tokens_used()
    state = legacy_state.load_state()
    assert state["total_input_tokens"] == 10_000
    assert state["total_output_tokens"] == 0
    assert state["total_cost_usd"] == 0.0
    assert state["runs"] == []
    print("test_shim_reads_through_budgets: OK")


def test_shim_record_run_adds_tokens() -> None:
    _reset()
    legacy_state.record_run("solve_issues", "#42", input_tokens=1_500, output_tokens=500)
    legacy_state.record_run("solve_issues", "#43", input_tokens=200, output_tokens=100)
    assert budgets.get_budgets().tokens_used == 2_300
    # Shim deliberately does NOT touch the tasks counter — intercept.py
    # owns that boundary.
    assert budgets.get_budgets().tasks_used == 0
    print("test_shim_record_run_adds_tokens: OK")


def test_shim_check_budget_respects_shared_counter() -> None:
    _reset()
    budgets.set_token_budget(1_000)
    budgets.add_tokens(800, 0)
    # Still under the caller's limit, but well under the shared counter
    assert legacy_state.check_budget(5_000) is True
    # Push past the shared limit — now check_budget must return False even
    # though the caller's max_tokens is much larger
    budgets.add_tokens(300, 0)
    assert legacy_state.check_budget(5_000) is False, (
        "shim must respect shared token counter when it's exhausted"
    )
    print("test_shim_check_budget_respects_shared_counter: OK")


def test_shim_reset_clears_all_counters() -> None:
    _reset()
    budgets.add_tokens(1_000, 0)
    budgets.increment_edits(3)
    budgets.increment_tasks(2)
    legacy_state.reset_state()
    b = budgets.get_budgets()
    assert b.tokens_used == 0
    assert b.edits_used == 0
    assert b.tasks_used == 0
    print("test_shim_reset_clears_all_counters: OK")


def test_shim_save_state_is_noop() -> None:
    _reset()
    budgets.add_tokens(500, 0)
    # save_state used to be the write path; now it's a no-op. Calling it
    # with a bogus dict must not corrupt the real state.
    legacy_state.save_state({"total_input_tokens": 999_999, "nonsense": True})
    assert budgets.get_budgets().tokens_used == 500
    print("test_shim_save_state_is_noop: OK")


def test_shim_get_run_count_maps_to_tasks() -> None:
    _reset()
    budgets.increment_tasks(4)
    assert legacy_state.get_run_count() == 4
    print("test_shim_get_run_count_maps_to_tasks: OK")


# ---------- runner ----------


def main() -> None:
    tests = [
        test_get_budgets_defaults_on_empty_state,
        test_get_budgets_to_dict_shape,
        test_set_limits_persist,
        test_set_limit_rejects_bad_inputs,
        test_increments_accumulate,
        test_add_tokens_rejects_negatives,
        test_reset_budgets_all,
        test_reset_budgets_selective,
        test_reset_budgets_rejects_bad_counter,
        test_exhausted_and_remaining,
        test_any_exhausted,
        test_per_invocation_cap,
        test_shim_reads_through_budgets,
        test_shim_record_run_adds_tokens,
        test_shim_check_budget_respects_shared_counter,
        test_shim_reset_clears_all_counters,
        test_shim_save_state_is_noop,
        test_shim_get_run_count_maps_to_tasks,
    ]
    for t in tests:
        t()
    print(f"\nAll {len(tests)} tests passed.")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"\nFAIL: {e}")
        sys.exit(1)
    except Exception as e:
        import traceback

        print(f"\nERROR: {type(e).__name__}: {e}")
        traceback.print_exc()
        sys.exit(1)
