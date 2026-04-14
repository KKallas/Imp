"""Tests for server/dispatcher.py.

Run directly: `.venv/bin/python tests/test_dispatcher.py`
No pytest. Asserts → exit 0 on success, exit 1 on failure.

Covers:
  - _parse_verdict: execute / clarify / answer happy paths + malformed
    JSON / missing fields / unknown type / empty response
  - _parse_explicit: run: / run / moderate / solve / fix patterns, plus
    inputs that should NOT be recognised as explicit
  - dispatch() end-to-end with a fake backend:
      - execute branch goes through intercept.execute_command
      - answer branch calls `say` and never touches intercept
      - clarify → answer flow: one question, one response, then execute
      - explicit-mode shortcut skips the backend entirely
      - token usage feeds into budgets.add_tokens
      - bounded clarify loop (too many clarifies → abort message)
      - backend error → say() with error message, no intercept call

Uses the same tempfile redirect trick as test_budgets.py so the shared
`.imp/state.json` is never touched.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server import budgets, dispatcher, guard, intercept  # noqa: E402

# Redirect state file + stub out the guard so intercept.execute_command
# runs without calling Claude. These tests exercise the dispatcher — the
# guard path is covered in test_guard.py.
_TMP_DIR = Path(tempfile.mkdtemp(prefix="imp-dispatcher-test-"))
budgets.STATE_FILE = _TMP_DIR / "state.json"


async def _auto_approve_guard(system: str, user: str) -> str:
    return '{"verdict": "approve", "reason": "test"}'


guard.set_backend(_auto_approve_guard)


# ---------- fake dispatcher backend ----------


class FakeBackend:
    """Returns scripted responses in order. Records every call."""

    def __init__(self, responses: list[dispatcher.BackendResult]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, system: str, user: str) -> dispatcher.BackendResult:
        self.calls.append((system, user))
        if not self.responses:
            raise AssertionError("FakeBackend ran out of scripted responses")
        return self.responses.pop(0)


# ---------- say / ask doubles ----------


class SayRecorder:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def __call__(self, text: str) -> None:
        self.messages.append(text)


class AskScript:
    """Scripted ask() — returns answers from a queue in order."""

    def __init__(self, answers: list[str | None]) -> None:
        self.answers = list(answers)
        self.questions: list[str] = []

    async def __call__(self, question: str) -> str | None:
        self.questions.append(question)
        if not self.answers:
            raise AssertionError("AskScript ran out of scripted answers")
        return self.answers.pop(0)


def _reset() -> None:
    if budgets.STATE_FILE.exists():
        budgets.STATE_FILE.unlink()
    dispatcher.set_backend(None)
    intercept.running_tasks.clear()
    intercept.action_log.clear()


# ---------- _parse_verdict ----------


def test_parse_verdict_execute() -> None:
    _reset()
    raw = '{"type": "execute", "argv": ["gh", "issue", "view", "42"], "rationale": "view 42"}'
    v = dispatcher._parse_verdict(raw)
    assert v.type == "execute", v
    assert v.argv == ["gh", "issue", "view", "42"]
    assert v.rationale == "view 42"
    print("test_parse_verdict_execute: OK")


def test_parse_verdict_clarify() -> None:
    _reset()
    raw = '{"type": "clarify", "question": "Which issue?"}'
    v = dispatcher._parse_verdict(raw)
    assert v.type == "clarify"
    assert v.question == "Which issue?"
    print("test_parse_verdict_clarify: OK")


def test_parse_verdict_answer() -> None:
    _reset()
    raw = '{"type": "answer", "text": "Budget is 200k tokens."}'
    v = dispatcher._parse_verdict(raw)
    assert v.type == "answer"
    assert v.text == "Budget is 200k tokens."
    print("test_parse_verdict_answer: OK")


def test_parse_verdict_code_fenced_json() -> None:
    _reset()
    raw = '```json\n{"type": "answer", "text": "ok"}\n```'
    v = dispatcher._parse_verdict(raw)
    assert v.type == "answer"
    assert v.text == "ok"
    print("test_parse_verdict_code_fenced_json: OK")


def test_parse_verdict_prose_around_json() -> None:
    _reset()
    raw = 'Here you go: {"type": "execute", "argv": ["echo", "hi"], "rationale": "test"} — done.'
    v = dispatcher._parse_verdict(raw)
    assert v.type == "execute"
    assert v.argv == ["echo", "hi"]
    print("test_parse_verdict_prose_around_json: OK")


def test_parse_verdict_missing_argv() -> None:
    _reset()
    raw = '{"type": "execute", "rationale": "missing argv"}'
    v = dispatcher._parse_verdict(raw)
    assert v.type == "error"
    assert "argv" in (v.error or "").lower()
    print("test_parse_verdict_missing_argv: OK")


def test_parse_verdict_argv_not_list_of_strings() -> None:
    _reset()
    raw = '{"type": "execute", "argv": ["gh", 42], "rationale": "bad"}'
    v = dispatcher._parse_verdict(raw)
    assert v.type == "error"
    print("test_parse_verdict_argv_not_list_of_strings: OK")


def test_parse_verdict_empty_question() -> None:
    _reset()
    raw = '{"type": "clarify", "question": ""}'
    v = dispatcher._parse_verdict(raw)
    assert v.type == "error"
    print("test_parse_verdict_empty_question: OK")


def test_parse_verdict_unknown_type() -> None:
    _reset()
    raw = '{"type": "shrug", "text": "dunno"}'
    v = dispatcher._parse_verdict(raw)
    assert v.type == "error"
    assert "unknown" in (v.error or "").lower()
    print("test_parse_verdict_unknown_type: OK")


def test_parse_verdict_empty() -> None:
    _reset()
    v = dispatcher._parse_verdict("")
    assert v.type == "error"
    print("test_parse_verdict_empty: OK")


def test_parse_verdict_garbage() -> None:
    _reset()
    v = dispatcher._parse_verdict("not JSON at all, just words")
    assert v.type == "error"
    print("test_parse_verdict_garbage: OK")


# ---------- _parse_explicit ----------


def test_parse_explicit_run_colon() -> None:
    _reset()
    assert dispatcher._parse_explicit("run: echo hello") == ["echo", "hello"]
    assert dispatcher._parse_explicit("RUN: gh issue view 42") == [
        "gh",
        "issue",
        "view",
        "42",
    ]
    print("test_parse_explicit_run_colon: OK")


def test_parse_explicit_run_space() -> None:
    _reset()
    assert dispatcher._parse_explicit("run echo hi") == ["echo", "hi"]
    print("test_parse_explicit_run_space: OK")


def test_parse_explicit_keywords() -> None:
    _reset()
    assert dispatcher._parse_explicit("moderate issue 42") == [
        "python",
        "99-tools/moderate_issues.py",
        "--issue",
        "42",
    ]
    assert dispatcher._parse_explicit("solve 7") == [
        "python",
        "99-tools/solve_issues.py",
        "--issue",
        "7",
    ]
    assert dispatcher._parse_explicit("fix pr #17") == [
        "python",
        "99-tools/fix_prs.py",
        "--pr",
        "17",
    ]
    print("test_parse_explicit_keywords: OK")


def test_parse_explicit_ignores_ambiguous() -> None:
    _reset()
    assert dispatcher._parse_explicit("what's the budget?") is None
    assert dispatcher._parse_explicit("moderate the issues for me") is None
    assert dispatcher._parse_explicit("") is None
    assert dispatcher._parse_explicit("run:") is None  # empty tail
    print("test_parse_explicit_ignores_ambiguous: OK")


# ---------- dispatch() ----------


async def test_dispatch_execute_branch() -> None:
    _reset()
    say = SayRecorder()
    ask = AskScript([])
    backend = FakeBackend(
        [
            # Round 1: classifier picks execute
            (
                '{"type": "execute", "argv": ["echo", "hello"], "rationale": "echoing"}',
                150,
                75,
            ),
            # Round 2 (P2.9b): synthesis interprets the output in prose
            (
                "The `echo hello` command printed 'hello' and exited successfully.",
                100,
                40,
            ),
        ]
    )
    dispatcher.set_backend(backend)

    await dispatcher.dispatch("say hello", say=say, ask=ask)

    # Both backend calls fired: classifier + synthesis
    assert len(backend.calls) == 2, backend.calls
    # Three say() calls now: narration, summary (output + exit code), synthesis
    assert len(say.messages) == 3, say.messages
    assert "Running:" in say.messages[0] and "echo hello" in say.messages[0]
    assert "hello" in say.messages[1]  # the echo output
    assert "Exit code: `0`" in say.messages[1]
    assert "printed 'hello'" in say.messages[2]
    # The synthesis call must have used the FOLLOWUP_SYSTEM_PROMPT
    assert backend.calls[1][0] == dispatcher.FOLLOWUP_SYSTEM_PROMPT
    # And its user prompt must include the original question + output
    synth_user = backend.calls[1][1]
    assert "say hello" in synth_user
    assert "hello" in synth_user
    assert "Exit code: 0" in synth_user
    # Intercept actually ran the echo
    assert len(intercept.action_log) == 1
    a = intercept.action_log[0]
    assert a.command == ["echo", "hello"]
    assert a.verdict == "approve"
    assert a.returncode == 0
    # Token counts from BOTH calls feed into budgets (225 + 140 = 365)
    b = budgets.get_budgets()
    assert b.tokens_used == 365, b.tokens_used
    print("test_dispatch_execute_branch: OK")


async def test_dispatch_answer_branch() -> None:
    _reset()
    say = SayRecorder()
    ask = AskScript([])
    backend = FakeBackend(
        [('{"type": "answer", "text": "Your token budget is 200,000."}', 80, 40)]
    )
    dispatcher.set_backend(backend)

    await dispatcher.dispatch("what's the budget?", say=say, ask=ask)

    assert len(backend.calls) == 1
    assert say.messages == ["Your token budget is 200,000."]
    # No intercept call — pure answer
    assert intercept.action_log == []
    # Token accounting still fires
    assert budgets.get_budgets().tokens_used == 120
    print("test_dispatch_answer_branch: OK")


async def test_dispatch_clarify_then_execute() -> None:
    _reset()
    say = SayRecorder()
    ask = AskScript(["42"])  # admin answers "42"
    backend = FakeBackend(
        [
            # Round 1: classifier asks for clarification
            ('{"type": "clarify", "question": "Which issue number?"}', 100, 20),
            # Round 2: with the answer, classifier picks execute
            (
                '{"type": "execute", "argv": ["gh", "issue", "view", "42"], '
                '"rationale": "view issue 42"}',
                150,
                30,
            ),
            # Round 3 (P2.9b): synthesis — note gh will fail in the test
            # env so `output` may be empty. The synthesis is skipped when
            # output is empty, so we only include this response in case
            # gh is actually authenticated. If unused, FakeBackend will
            # complain on shutdown — use a short-circuit below.
            (
                "Issue 42 could not be viewed — gh returned an error.",
                80,
                30,
            ),
        ]
    )
    dispatcher.set_backend(backend)

    await dispatcher.dispatch("view an issue", say=say, ask=ask)

    # At least 2 backend calls (clarifier + executor). A third only fires
    # if gh produced output to synthesize.
    assert len(backend.calls) >= 2
    assert ask.questions == ["Which issue number?"]
    # The second backend call must contain the clarification history
    second_user_prompt = backend.calls[1][1]
    assert "CLARIFICATION SO FAR" in second_user_prompt
    assert "42" in second_user_prompt
    # Intercept actually ran (although it'll fail to exec gh in the test
    # environment — we care about verdict / classification, not success)
    assert len(intercept.action_log) == 1
    a = intercept.action_log[0]
    assert a.command == ["gh", "issue", "view", "42"]
    assert a.classified_as == "read"
    # Execute branch emits narration + summary
    assert any("Running:" in m for m in say.messages), say.messages
    print("test_dispatch_clarify_then_execute: OK")


async def test_dispatch_synthesis_skipped_on_reject() -> None:
    """Budget/guard rejection → no output → no synthesis call."""
    _reset()
    # Zero out edits so intercept rejects any write
    budgets.set_limit("edits", 0)
    say = SayRecorder()
    ask = AskScript([])
    backend = FakeBackend(
        [
            (
                '{"type": "execute", "argv": ["gh", "issue", "edit", "42", '
                '"--add-label", "foo"], "rationale": "add label"}',
                120,
                40,
            ),
            # If synthesis fires, it would consume this — but it shouldn't.
            ("UNEXPECTED SYNTHESIS", 10, 10),
        ]
    )
    dispatcher.set_backend(backend)

    try:
        await dispatcher.dispatch("add label foo to issue 42", say=say, ask=ask)
    finally:
        budgets.set_limit("edits", budgets.DEFAULT_LIMITS["edits"])

    # Only the classifier call should have fired
    assert len(backend.calls) == 1, backend.calls
    # Summary shows rejection; no synthesis message follows
    assert any("Rejected" in m for m in say.messages), say.messages
    assert not any("UNEXPECTED SYNTHESIS" in m for m in say.messages)
    print("test_dispatch_synthesis_skipped_on_reject: OK")


async def test_dispatch_synthesis_skipped_on_empty_output() -> None:
    """A command that exits 0 with no stdout/stderr doesn't need synthesis."""
    _reset()
    say = SayRecorder()
    ask = AskScript([])
    backend = FakeBackend(
        [
            # `true` produces no output. Demo-safe list doesn't include
            # it, but `date` with stdout suppressed is awkward — use `:`
            # via echo with empty arg. Actually the cleanest: `echo`
            # with no args prints a newline only. To get truly empty
            # output, use `sleep 0`.
            (
                '{"type": "execute", "argv": ["sleep", "0"], "rationale": "no-op"}',
                100,
                30,
            ),
            # If synthesis fires this is consumed; it shouldn't.
            ("UNEXPECTED SYNTHESIS", 10, 10),
        ]
    )
    dispatcher.set_backend(backend)

    await dispatcher.dispatch("do nothing", say=say, ask=ask)

    # `sleep 0` is in DEMO_SAFE_COMMANDS → classified as read → no guard,
    # no budget. It produces no output, exit 0. Synthesis must skip.
    assert len(backend.calls) == 1, backend.calls
    assert not any("UNEXPECTED SYNTHESIS" in m for m in say.messages)
    print("test_dispatch_synthesis_skipped_on_empty_output: OK")


async def test_dispatch_thinking_brackets_synthesis_call() -> None:
    """When `thinking` is provided, it must be entered BEFORE the synthesis
    backend call and exited AFTER — so the UI spinner actually covers the
    slow part. Empty-output and rejected paths skip synthesis entirely,
    so `thinking` must NOT be entered in those cases either."""
    _reset()

    events: list[tuple[str, str | None]] = []

    class ThinkingRecorder:
        def __init__(self, label: str) -> None:
            self.label = label

        async def __aenter__(self):
            events.append(("enter", self.label))
            return self

        async def __aexit__(self, exc_type, exc, tb):
            events.append(("exit", self.label))
            return False

    def thinking_factory(label: str) -> ThinkingRecorder:
        return ThinkingRecorder(label)

    say = SayRecorder()
    ask = AskScript([])

    async def tracking_backend(system: str, user: str) -> dispatcher.BackendResult:
        events.append(("backend", system[:30]))
        if system.startswith("You are Foreman, the chat agent"):
            # First call — classifier picks execute
            return (
                '{"type": "execute", "argv": ["echo", "tracked"], "rationale": "trace"}',
                40,
                20,
            )
        # Second call — synthesis
        return ("I counted 'tracked' in the output.", 30, 15)

    dispatcher.set_backend(tracking_backend)

    await dispatcher.dispatch(
        "trace the flow",
        say=say,
        ask=ask,
        thinking=thinking_factory,
    )

    # Synthesis message landed
    assert any("counted 'tracked'" in m for m in say.messages), say.messages

    # Event order must be:
    #   backend (classifier) → backend (synthesis) wrapped by enter/exit
    # i.e. the thinking block brackets ONLY the synthesis call.
    backend_events = [e for e in events if e[0] == "backend"]
    thinking_events = [e for e in events if e[0] in ("enter", "exit")]

    assert len(backend_events) == 2, backend_events
    assert len(thinking_events) == 2, thinking_events
    assert thinking_events[0][0] == "enter"
    assert thinking_events[1][0] == "exit"
    assert "Interpreting" in thinking_events[0][1] or "output" in thinking_events[0][1].lower()

    # Strict ordering: classifier backend call comes first, then enter,
    # then synthesis backend, then exit.
    ordered_kinds = [e[0] for e in events]
    assert ordered_kinds == ["backend", "enter", "backend", "exit"], ordered_kinds

    print("test_dispatch_thinking_brackets_synthesis_call: OK")


async def test_dispatch_thinking_not_entered_when_synthesis_skipped() -> None:
    """No synthesis → no thinking enter/exit. Covers the reject path."""
    _reset()
    budgets.set_limit("edits", 0)

    entered = []

    class Recorder:
        def __init__(self, label: str):
            self.label = label

        async def __aenter__(self):
            entered.append(self.label)

        async def __aexit__(self, *a):
            return False

    say = SayRecorder()
    ask = AskScript([])
    backend = FakeBackend(
        [
            (
                '{"type": "execute", "argv": ["gh", "issue", "edit", "42", '
                '"--add-label", "x"], "rationale": "add label"}',
                50,
                20,
            )
        ]
    )
    dispatcher.set_backend(backend)

    try:
        await dispatcher.dispatch(
            "add label x to issue 42",
            say=say,
            ask=ask,
            thinking=lambda label: Recorder(label),
        )
    finally:
        budgets.set_limit("edits", budgets.DEFAULT_LIMITS["edits"])

    # Rejected → no synthesis → thinking factory never called
    assert entered == [], entered
    print("test_dispatch_thinking_not_entered_when_synthesis_skipped: OK")


async def test_dispatch_synthesis_failure_is_soft() -> None:
    """If the synthesis backend call raises, the admin still gets the raw output."""
    _reset()
    say = SayRecorder()
    ask = AskScript([])

    call_count = {"n": 0}

    async def flaky_backend(system, user):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return (
                '{"type": "execute", "argv": ["echo", "hi"], "rationale": "say hi"}',
                50,
                25,
            )
        raise RuntimeError("synthesis backend is having a bad day")

    dispatcher.set_backend(flaky_backend)

    await dispatcher.dispatch("say hi", say=say, ask=ask)

    # Narration + summary are still posted, even though synthesis threw
    assert any("Running:" in m for m in say.messages), say.messages
    assert any("Exit code: `0`" in m for m in say.messages), say.messages
    # The action ran successfully despite the failed synthesis
    assert len(intercept.action_log) == 1
    assert intercept.action_log[0].returncode == 0
    print("test_dispatch_synthesis_failure_is_soft: OK")


async def test_dispatch_explicit_shortcut_skips_backend() -> None:
    _reset()
    say = SayRecorder()
    ask = AskScript([])
    # FakeBackend with no scripted responses — if the dispatcher calls
    # the backend, the test fails loudly.
    backend = FakeBackend([])
    dispatcher.set_backend(backend)

    await dispatcher.dispatch("run: echo explicit", say=say, ask=ask)

    assert backend.calls == [], "explicit-mode dispatch should skip the LLM"
    assert len(intercept.action_log) == 1
    assert intercept.action_log[0].command == ["echo", "explicit"]
    # No tokens burned because no LLM call
    assert budgets.get_budgets().tokens_used == 0
    # Narration + summary after execution, even on explicit path.
    assert len(say.messages) == 2, say.messages
    assert "Running:" in say.messages[0] and "explicit" in say.messages[0].lower()
    assert "Exit code: `0`" in say.messages[1]
    print("test_dispatch_explicit_shortcut_skips_backend: OK")


async def test_dispatch_clarify_loop_bounded() -> None:
    _reset()
    say = SayRecorder()
    # Admin answers the same "42" to every clarify
    ask = AskScript(["42"] * dispatcher.MAX_CLARIFY_TURNS)
    # Backend always asks for more clarification
    backend = FakeBackend(
        [('{"type": "clarify", "question": "Still unclear, try again?"}', 50, 20)]
        * dispatcher.MAX_CLARIFY_TURNS
    )
    dispatcher.set_backend(backend)

    await dispatcher.dispatch("something", say=say, ask=ask)

    # Exactly MAX_CLARIFY_TURNS backend calls, then abort message
    assert len(backend.calls) == dispatcher.MAX_CLARIFY_TURNS
    assert len(say.messages) == 1
    assert "rephrasing" in say.messages[0].lower() or "clarification" in say.messages[0].lower()
    # No intercept call — we never reached execute
    assert intercept.action_log == []
    print("test_dispatch_clarify_loop_bounded: OK")


async def test_dispatch_ask_timeout_aborts() -> None:
    _reset()
    say = SayRecorder()
    ask = AskScript([None])  # admin times out
    backend = FakeBackend(
        [('{"type": "clarify", "question": "Which one?"}', 10, 5)]
    )
    dispatcher.set_backend(backend)

    await dispatcher.dispatch("do something", say=say, ask=ask)

    assert len(backend.calls) == 1
    assert len(say.messages) == 1
    assert "no response" in say.messages[0].lower()
    assert intercept.action_log == []
    print("test_dispatch_ask_timeout_aborts: OK")


async def test_dispatch_backend_error() -> None:
    _reset()
    say = SayRecorder()
    ask = AskScript([])

    async def broken_backend(system: str, user: str) -> dispatcher.BackendResult:
        raise RuntimeError("network down")

    dispatcher.set_backend(broken_backend)

    await dispatcher.dispatch("hello", say=say, ask=ask)

    assert len(say.messages) == 1
    assert "error" in say.messages[0].lower()
    assert intercept.action_log == []
    print("test_dispatch_backend_error: OK")


async def test_dispatch_unparseable_verdict() -> None:
    _reset()
    say = SayRecorder()
    ask = AskScript([])
    backend = FakeBackend([("not JSON, just words", 30, 15)])
    dispatcher.set_backend(backend)

    await dispatcher.dispatch("hello", say=say, ask=ask)

    assert len(say.messages) == 1
    assert "parse" in say.messages[0].lower() or "couldn't" in say.messages[0].lower()
    # Tokens still counted — the call itself happened
    assert budgets.get_budgets().tokens_used == 45
    assert intercept.action_log == []
    print("test_dispatch_unparseable_verdict: OK")


# ---------- runner ----------


async def amain() -> None:
    # sync tests
    test_parse_verdict_execute()
    test_parse_verdict_clarify()
    test_parse_verdict_answer()
    test_parse_verdict_code_fenced_json()
    test_parse_verdict_prose_around_json()
    test_parse_verdict_missing_argv()
    test_parse_verdict_argv_not_list_of_strings()
    test_parse_verdict_empty_question()
    test_parse_verdict_unknown_type()
    test_parse_verdict_empty()
    test_parse_verdict_garbage()
    test_parse_explicit_run_colon()
    test_parse_explicit_run_space()
    test_parse_explicit_keywords()
    test_parse_explicit_ignores_ambiguous()

    # async tests
    await test_dispatch_execute_branch()
    await test_dispatch_answer_branch()
    await test_dispatch_clarify_then_execute()
    await test_dispatch_synthesis_skipped_on_reject()
    await test_dispatch_synthesis_skipped_on_empty_output()
    await test_dispatch_thinking_brackets_synthesis_call()
    await test_dispatch_thinking_not_entered_when_synthesis_skipped()
    await test_dispatch_synthesis_failure_is_soft()
    await test_dispatch_explicit_shortcut_skips_backend()
    await test_dispatch_clarify_loop_bounded()
    await test_dispatch_ask_timeout_aborts()
    await test_dispatch_backend_error()
    await test_dispatch_unparseable_verdict()

    print("\nAll dispatcher tests passed.")


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
