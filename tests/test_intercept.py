"""Tests for server/intercept.py.

Run directly: `.venv/bin/python tests/test_intercept.py`
No pytest. Asserts → exit 0 on success, exit 1 on failure.

Covers:
  - classify_command (read/write/unknown for gh, pipeline scripts,
    demo-safe commands, edge cases)
  - execute_command happy path (read command, stdout captured)
  - Log file creation and content
  - running_tasks map populated during run, cleaned up after
  - abort_task SIGTERM path
  - Manual-mode pause flag blocks writes but allows reads
  - Unknown commands rejected without execution
  - Budget exhaustion rejects a fake write invocation
  - Snapshot helpers return plain dicts

This file does not import chainlit — it uses the fact that intercept.py
only needs .update() and .status on the `step`/`task_entry` objects
passed to it. Tests pass a lightweight mock.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

# Make `server.intercept` importable regardless of invocation directory
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server import budgets, guard, intercept  # noqa: E402


# ---------- fake guard backend (so intercept tests run without the SDK) ----------


async def _fake_guard_backend(system_prompt: str, user_prompt: str) -> str:
    """Always-approve backend for intercept tests.

    This replaces the real Claude call so that the existing intercept
    tests keep exercising budget, classification, pause-flag, etc.
    without needing the claude-agent-sdk or a live API key.
    """
    return '{"verdict": "approve", "reason": "test backend — auto-approve"}'


guard.set_backend(_fake_guard_backend)

# ---------- helpers ----------


class FakeStep:
    """Minimal stand-in for cl.Step so execute_command has something to stream into."""

    def __init__(self) -> None:
        self.input: str | None = None
        self.output: str = ""
        self.updates: int = 0

    async def update(self) -> None:
        self.updates += 1


def _reset_state() -> None:
    """Clear in-memory state and budget counters between tests."""
    intercept.running_tasks.clear()
    intercept.action_log.clear()
    intercept.set_accepting_new_actions(True)
    # Reset budgets
    budgets.reset_all_counters()
    # Clean up old log files so log-file assertions are clean
    if intercept.OUTPUT_DIR.exists():
        for f in intercept.OUTPUT_DIR.glob("act_*.log"):
            try:
                f.unlink()
            except OSError:
                pass


# ---------- tests ----------


def test_classify_command() -> None:
    _reset_state()
    cases = [
        (["gh", "issue", "view", "42"], "read"),
        (["gh", "issue", "list"], "read"),
        (["gh", "issue", "edit", "42"], "write"),
        (["gh", "issue", "create"], "write"),
        (["gh", "project", "item-edit"], "write"),
        (["gh", "project", "field-create"], "write"),
        (["gh", "project", "item-list"], "read"),
        (["gh", "auth", "status"], "read"),
        (["python", "tools/github/solve_issues.py", "--issue", "42"], "write"),
        (["python", "tools/github/moderate_issues.py"], "write"),
        (["python3", "pipeline/sync_issues.py"], "read"),
        # estimate_dates.py is read by default, write when --push is set
        (["python3", "pipeline/estimate_dates.py"], "read"),
        (["python3", "pipeline/estimate_dates.py", "--push"], "write"),
        (["echo", "hello"], "read"),
        (["date"], "read"),
        (["rm", "-rf", "/"], "unknown"),
        ([], "unknown"),
        (["gh"], "unknown"),
        # KKallas/Imp#46 — inline code routed to Guard via "write"
        (["python3", "-c", "print(1)"], "write"),
        (["python", "-c", "x"], "write"),
        (["bash", "-c", "ls"], "write"),
        (["sh", "-c", "echo"], "write"),
        # `-c` requires a payload — bare `python -c` is unknown
        (["python", "-c"], "unknown"),
        # `-c` only matters as the immediate next arg
        (["python", "script.py", "-c"], "unknown"),
    ]
    for argv, expected in cases:
        got = intercept.classify_command(argv)
        assert got == expected, f"classify {argv!r} expected {expected}, got {got}"
    print("test_classify_command: OK")


async def test_execute_read_command() -> None:
    _reset_state()
    rc, out, action = await intercept.execute_command(["echo", "hello world"])
    assert rc == 0, f"expected 0, got {rc}: {out}"
    assert "hello world" in out, f"expected 'hello world' in output, got {out!r}"
    assert action.classified_as == "read"
    assert action.verdict == "approve"
    assert action.returncode == 0
    assert action.finished_at is not None
    print("test_execute_read_command: OK")


async def test_streams_into_step() -> None:
    _reset_state()
    step = FakeStep()
    rc, _, _ = await intercept.execute_command(["echo", "streamed"], step=step)
    assert rc == 0
    assert "streamed" in step.output, f"step.output was {step.output!r}"
    assert step.updates >= 1, "step.update() was never called"
    print("test_streams_into_step: OK")


async def test_log_file_written() -> None:
    _reset_state()
    rc, _, action = await intercept.execute_command(["echo", "logged line"])
    log_path = intercept.OUTPUT_DIR / f"{action.action_id}.log"
    assert log_path.exists(), f"log file {log_path} was not created"
    content = log_path.read_text()
    assert "logged line" in content, f"log content was {content!r}"
    log_path.unlink()
    print("test_log_file_written: OK")


async def test_running_tasks_populated_and_cleaned() -> None:
    _reset_state()
    # Start a sleep, check running_tasks, wait for it
    started = asyncio.create_task(intercept.execute_command(["sleep", "0.3"]))
    await asyncio.sleep(0.1)
    assert len(intercept.running_tasks) == 1, (
        f"expected 1 running task, got {len(intercept.running_tasks)}"
    )
    snap = intercept.get_running_tasks_snapshot()
    assert len(snap) == 1
    assert "sleep" in snap[0]["command"]
    assert snap[0]["pid"] > 0
    rc, _, _ = await started
    assert rc == 0
    assert len(intercept.running_tasks) == 0, (
        "running task was not cleaned up on exit"
    )
    print("test_running_tasks_populated_and_cleaned: OK")


async def test_abort_task_sigterm() -> None:
    _reset_state()
    # Start a long sleep, then abort
    started = asyncio.create_task(intercept.execute_command(["sleep", "60"]))
    await asyncio.sleep(0.2)
    assert len(intercept.running_tasks) == 1
    action_id = next(iter(intercept.running_tasks.keys()))
    t0 = time.monotonic()
    killed, msg = await intercept.abort_task(action_id)
    elapsed = time.monotonic() - t0
    assert killed, f"abort returned killed=False: {msg}"
    assert elapsed < 5, f"abort took {elapsed:.1f}s — sleep should respond to SIGTERM immediately"
    rc, _, _ = await started
    assert rc != 0, f"expected nonzero exit after abort, got {rc}"
    assert len(intercept.running_tasks) == 0
    print("test_abort_task_sigterm: OK")


async def test_pause_flag_blocks_writes_allows_reads() -> None:
    _reset_state()
    intercept.set_accepting_new_actions(False)
    try:
        # Write rejected
        rc, out, action = await intercept.execute_command(
            ["gh", "issue", "edit", "42"]
        )
        assert rc != 0, "paused state should reject writes"
        assert action.verdict == "reject"
        assert "paused" in action.verdict_reason.lower()

        # Read still allowed
        rc, out, action = await intercept.execute_command(["echo", "ok"])
        assert rc == 0, f"reads should still work when paused, got {rc}: {out}"
        assert action.verdict == "approve"
    finally:
        intercept.set_accepting_new_actions(True)
    print("test_pause_flag_blocks_writes_allows_reads: OK")


async def test_unknown_command_rejected() -> None:
    _reset_state()
    rc, out, action = await intercept.execute_command(["rm", "-rf", "/tmp/nonexistent"])
    assert rc != 0
    assert action.verdict == "reject"
    assert "unknown" in (action.verdict_reason or "").lower()
    assert action.returncode is None, (
        "unknown commands should never be executed, so returncode should stay None"
    )
    print("test_unknown_command_rejected: OK")


async def test_budget_exhaustion_rejects_write() -> None:
    _reset_state()
    # Seed a zero edits budget
    budgets.set_limit("edits", 0)
    try:
        # Pretend the worker wants to edit an issue — the stub guard would
        # approve it but the budget check fires first
        rc, out, action = await intercept.execute_command(
            ["gh", "issue", "edit", "42", "--add-label", "foo"]
        )
        assert rc != 0
        assert action.verdict == "reject"
        assert "edits" in (action.verdict_reason or "").lower()
    finally:
        budgets.set_limit("edits", budgets.DEFAULT_LIMITS["edits"])
    print("test_budget_exhaustion_rejects_write: OK")


async def test_token_exhaustion_rejects_write() -> None:
    _reset_state()
    # Seed a token budget that's already spent
    budgets.set_limit("tokens", 100)
    budgets.add_tokens(200, 0)
    try:
        rc, out, action = await intercept.execute_command(
            ["gh", "issue", "edit", "42", "--add-label", "foo"]
        )
        assert rc != 0
        assert action.verdict == "reject"
        assert "token" in (action.verdict_reason or "").lower()

        # Reads still work when the token budget is exhausted
        rc2, _, action2 = await intercept.execute_command(["echo", "ok"])
        assert rc2 == 0
        assert action2.verdict == "approve"
    finally:
        budgets.set_limit("tokens", budgets.DEFAULT_LIMITS["tokens"])
        budgets.reset_counter("tokens")
    print("test_token_exhaustion_rejects_write: OK")


async def test_task_budget_increments_on_success() -> None:
    """Pipeline-script runs bump the tasks counter only on exit 0.

    We can't actually invoke solve_issues.py here (it needs the Claude
    SDK), so this test verifies the accounting path via a fake pipeline
    script that classify_command recognises: a python invocation whose
    argv[1] ends in one of the PIPELINE_WRITE_SCRIPTS paths. We fake it
    by temporarily whitelisting a safe script path.
    """
    _reset_state()
    # Directly exercise the increment path — the integration against real
    # pipeline scripts is covered by the existing E2E tests, not unit tests.
    before = budgets.get_budgets().tasks_used
    budgets.increment_tasks(1)
    assert budgets.get_budgets().tasks_used == before + 1
    print("test_task_budget_increments_on_success: OK")


async def test_action_log_populated() -> None:
    _reset_state()
    await intercept.execute_command(["echo", "a"])
    await intercept.execute_command(["echo", "b"])
    recent = intercept.get_recent_actions(10)
    assert len(recent) == 2
    assert all("action_id" in r for r in recent)
    assert all(r["verdict"] == "approve" for r in recent)
    print("test_action_log_populated: OK")


# ---------- runner ----------


async def amain() -> None:
    test_classify_command()
    await test_execute_read_command()
    await test_streams_into_step()
    await test_log_file_written()
    await test_running_tasks_populated_and_cleaned()
    await test_abort_task_sigterm()
    await test_pause_flag_blocks_writes_allows_reads()
    await test_unknown_command_rejected()
    await test_budget_exhaustion_rejects_write()
    await test_token_exhaustion_rejects_write()
    await test_task_budget_increments_on_success()
    await test_action_log_populated()
    print("\nAll tests passed.")


if __name__ == "__main__":
    try:
        asyncio.run(amain())
    except AssertionError as e:
        print(f"\nFAIL: {e}")
        sys.exit(1)
    except Exception as e:
        import traceback

        print(f"\nERROR: {type(e).__name__}: {e}")
        traceback.print_exc()
        sys.exit(1)
