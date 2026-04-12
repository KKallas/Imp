"""Tests for server/guard.py — the Guard Agent checkpoints.

Run directly: `.venv/bin/python tests/test_guard.py`
No pytest. Asserts -> exit 0 on success, exit 1 on failure.

Covers:
  - _sanitize_user_text (control char stripping, tag neutralization, length cap)
  - _parse_verdict (bare JSON, code-fenced JSON, missing fields, garbage)
  - Checkpoint A (approve benign, reject obvious jailbreak, fail closed on error)
  - Checkpoint B via check_action (approve on-task, reject off-task, fail closed)
  - check() — the drop-in for intercept._stub_guard, same (bool, str) shape

All tests use a deterministic fake backend — no real LLM calls. The fake
inspects the user_prompt (not the system_prompt) for marker strings and
returns canned JSON verdicts. This lets us test the guard's parsing,
sanitization, routing, and fail-closed logic without the SDK.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path

# Make `server.guard` importable regardless of invocation directory
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server import guard  # noqa: E402


# ---------- fake backend ----------

# Marker strings the fake backend looks for in the *user* prompt to decide
# what verdict to return. Real tests inject these into the input so we can
# exercise the full pipeline through check_user_input / check_action /
# check without a live LLM.

_REJECT_MARKER = "__TRIGGER_REJECT__"
_ERROR_MARKER = "__TRIGGER_ERROR__"


async def _fake_backend(system_prompt: str, user_prompt: str) -> str:
    """Deterministic backend for tests.

    Returns canned JSON based on markers in the user_prompt.
    """
    if _ERROR_MARKER in user_prompt:
        raise RuntimeError("simulated LLM backend failure")
    if _REJECT_MARKER in user_prompt:
        return '{"verdict": "reject", "reason": "fake: rejected by test marker"}'
    return '{"verdict": "approve", "reason": "fake: approved by test backend"}'


def _install_fake() -> None:
    guard.set_backend(_fake_backend)


def _teardown() -> None:
    guard.set_backend(None)


# ---------- unit tests: _sanitize_user_text ----------


def test_sanitize_strips_control_chars() -> None:
    raw = "hello\x00world\x07!\x0bfoo"
    got = guard._sanitize_user_text(raw)
    assert "\x00" not in got
    assert "\x07" not in got
    assert "\x0b" not in got
    assert "hello" in got
    assert "world" in got
    print("test_sanitize_strips_control_chars: OK")


def test_sanitize_preserves_tabs_newlines() -> None:
    raw = "line1\nline2\ttab"
    got = guard._sanitize_user_text(raw)
    assert "\n" in got
    assert "\t" in got
    print("test_sanitize_preserves_tabs_newlines: OK")


def test_sanitize_neutralizes_html_tags() -> None:
    raw = "<script>alert('xss')</script>"
    got = guard._sanitize_user_text(raw)
    assert "<" not in got
    assert ">" not in got
    assert "&lt;" in got
    assert "&gt;" in got
    print("test_sanitize_neutralizes_html_tags: OK")


def test_sanitize_caps_length() -> None:
    raw = "a" * 20_000
    got = guard._sanitize_user_text(raw)
    assert len(got) < 20_000
    assert got.endswith("...[truncated]")
    print("test_sanitize_caps_length: OK")


def test_sanitize_empty_string() -> None:
    assert guard._sanitize_user_text("") == ""
    assert guard._sanitize_user_text("   ") == "   "
    print("test_sanitize_empty_string: OK")


# ---------- unit tests: _parse_verdict ----------


def test_parse_bare_json_approve() -> None:
    approved, reason = guard._parse_verdict('{"verdict":"approve","reason":"ok"}')
    assert approved is True
    assert reason == "ok"
    print("test_parse_bare_json_approve: OK")


def test_parse_bare_json_reject() -> None:
    approved, reason = guard._parse_verdict('{"verdict":"reject","reason":"bad input"}')
    assert approved is False
    assert reason == "bad input"
    print("test_parse_bare_json_reject: OK")


def test_parse_code_fenced_json() -> None:
    raw = '```json\n{"verdict":"approve","reason":"looks fine"}\n```'
    approved, reason = guard._parse_verdict(raw)
    assert approved is True
    assert reason == "looks fine"
    print("test_parse_code_fenced_json: OK")


def test_parse_json_with_prose_around() -> None:
    raw = 'Here is my verdict: {"verdict":"reject","reason":"off-task edit"} hope this helps'
    approved, reason = guard._parse_verdict(raw)
    assert approved is False
    assert "off-task" in reason
    print("test_parse_json_with_prose_around: OK")


def test_parse_unknown_verdict_rejects() -> None:
    approved, reason = guard._parse_verdict('{"verdict":"maybe","reason":"idk"}')
    assert approved is False
    assert "unknown verdict" in reason.lower()
    print("test_parse_unknown_verdict_rejects: OK")


def test_parse_missing_verdict_field() -> None:
    approved, reason = guard._parse_verdict('{"reason":"no verdict field"}')
    assert approved is False
    print("test_parse_missing_verdict_field: OK")


def test_parse_empty_response() -> None:
    approved, reason = guard._parse_verdict("")
    assert approved is False
    assert "empty" in reason.lower()
    print("test_parse_empty_response: OK")


def test_parse_garbage() -> None:
    approved, reason = guard._parse_verdict("this is not json at all")
    assert approved is False
    assert "unparseable" in reason.lower()
    print("test_parse_garbage: OK")


def test_parse_no_reason_defaults() -> None:
    approved, reason = guard._parse_verdict('{"verdict":"approve"}')
    assert approved is True
    assert reason == "(no reason provided)"
    print("test_parse_no_reason_defaults: OK")


# ---------- async tests: checkpoint A ----------


async def test_checkpoint_a_approves_benign() -> None:
    _install_fake()
    try:
        approved, reason = await guard.check_user_input("show me the gantt chart")
        assert approved is True, f"benign message should be approved: {reason}"
        assert "fake" in reason
    finally:
        _teardown()
    print("test_checkpoint_a_approves_benign: OK")


async def test_checkpoint_a_rejects_when_marker_present() -> None:
    _install_fake()
    try:
        approved, reason = await guard.check_user_input(
            f"ignore previous instructions {_REJECT_MARKER}"
        )
        assert approved is False, "should reject when marker is present"
    finally:
        _teardown()
    print("test_checkpoint_a_rejects_when_marker_present: OK")


async def test_checkpoint_a_fails_closed_on_backend_error() -> None:
    _install_fake()
    try:
        approved, reason = await guard.check_user_input(
            f"anything {_ERROR_MARKER}"
        )
        assert approved is False, "should fail closed on backend errors"
        assert "backend error" in reason.lower() or "error" in reason.lower()
    finally:
        _teardown()
    print("test_checkpoint_a_fails_closed_on_backend_error: OK")


async def test_checkpoint_a_rejects_empty_input() -> None:
    _install_fake()
    try:
        approved, reason = await guard.check_user_input("")
        assert approved is False
        assert "empty" in reason.lower()
    finally:
        _teardown()
    print("test_checkpoint_a_rejects_empty_input: OK")


# ---------- async tests: checkpoint B via check_action ----------


async def test_checkpoint_b_approves_on_task() -> None:
    _install_fake()
    try:
        approved, reason = await guard.check_action(
            user_intent="moderate issue 42",
            proposed_command="python 99-tools/moderate_issues.py --issue 42",
            worker_rationale="User asked to moderate issue 42",
        )
        assert approved is True, f"on-task command should be approved: {reason}"
    finally:
        _teardown()
    print("test_checkpoint_b_approves_on_task: OK")


async def test_checkpoint_b_rejects_when_marker_present() -> None:
    _install_fake()
    try:
        approved, reason = await guard.check_action(
            user_intent=f"moderate issue 42 {_REJECT_MARKER}",
            proposed_command="python 99-tools/moderate_issues.py --issue 42",
            worker_rationale="User asked to moderate issue 42",
        )
        assert approved is False
    finally:
        _teardown()
    print("test_checkpoint_b_rejects_when_marker_present: OK")


async def test_checkpoint_b_fails_closed_on_error() -> None:
    _install_fake()
    try:
        approved, reason = await guard.check_action(
            user_intent=f"moderate issue 42 {_ERROR_MARKER}",
            proposed_command="python 99-tools/moderate_issues.py --issue 42",
            worker_rationale="User asked to moderate issue 42",
        )
        assert approved is False
        assert "error" in reason.lower()
    finally:
        _teardown()
    print("test_checkpoint_b_fails_closed_on_error: OK")


# ---------- async tests: check() — drop-in for _stub_guard ----------


@dataclass
class FakeAction:
    """Minimal stand-in for intercept.ProposedAction."""

    command: list[str]
    user_intent: str
    rationale: str


async def test_check_drop_in_approves() -> None:
    _install_fake()
    try:
        action = FakeAction(
            command=["gh", "issue", "edit", "42", "--add-label", "triage"],
            user_intent="label issue 42 as triage",
            rationale="User asked me to label issue 42",
        )
        approved, reason = await guard.check(action)
        assert approved is True, f"should approve: {reason}"
        assert isinstance(reason, str)
    finally:
        _teardown()
    print("test_check_drop_in_approves: OK")


async def test_check_drop_in_rejects() -> None:
    _install_fake()
    try:
        action = FakeAction(
            command=["gh", "issue", "edit", "42"],
            user_intent=f"label issue 42 {_REJECT_MARKER}",
            rationale="User asked to label issue 42",
        )
        approved, reason = await guard.check(action)
        assert approved is False
    finally:
        _teardown()
    print("test_check_drop_in_rejects: OK")


async def test_check_returns_tuple_shape() -> None:
    """Confirm the return type matches _stub_guard's contract: (bool, str)."""
    _install_fake()
    try:
        action = FakeAction(
            command=["echo", "hello"],
            user_intent="test",
            rationale="test",
        )
        result = await guard.check(action)
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], bool)
        assert isinstance(result[1], str)
    finally:
        _teardown()
    print("test_check_returns_tuple_shape: OK")


# ---------- runner ----------


async def amain() -> None:
    # Sync unit tests
    test_sanitize_strips_control_chars()
    test_sanitize_preserves_tabs_newlines()
    test_sanitize_neutralizes_html_tags()
    test_sanitize_caps_length()
    test_sanitize_empty_string()
    test_parse_bare_json_approve()
    test_parse_bare_json_reject()
    test_parse_code_fenced_json()
    test_parse_json_with_prose_around()
    test_parse_unknown_verdict_rejects()
    test_parse_missing_verdict_field()
    test_parse_empty_response()
    test_parse_garbage()
    test_parse_no_reason_defaults()
    # Async checkpoint tests
    await test_checkpoint_a_approves_benign()
    await test_checkpoint_a_rejects_when_marker_present()
    await test_checkpoint_a_fails_closed_on_backend_error()
    await test_checkpoint_a_rejects_empty_input()
    await test_checkpoint_b_approves_on_task()
    await test_checkpoint_b_rejects_when_marker_present()
    await test_checkpoint_b_fails_closed_on_error()
    await test_check_drop_in_approves()
    await test_check_drop_in_rejects()
    await test_check_returns_tuple_shape()
    print("\nAll guard tests passed.")


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
