"""Tests for the structured turn UI (KKallas/Imp#55).

Run directly: `.venv/bin/python tests/test_foreman_turn_ui.py`
No pytest.  Asserts → exit 0 on success, exit 1 on failure.

Verifies the ``TurnUI`` / ``_ToolTracker`` callback sequences by
feeding fake ``AssistantMessage`` / ``ResultMessage`` streams through
``dispatch`` (with a fake SDK client) and asserting the callback log.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.foreman_agent import (  # noqa: E402
    PlanItem,
    TurnUI,
    _ToolTracker,
    _clean_tool_name,
    _format_tool_sig,
)


# ---------- helpers --------------------------------------------------


@dataclass
class _Event:
    kind: str
    args: dict[str, Any] = field(default_factory=dict)


class RecordingUI(TurnUI):
    """TurnUI that records every callback as an ``_Event``."""

    def __init__(self) -> None:
        self.events: list[_Event] = []

    async def show_plan(self, items: list[PlanItem]) -> None:
        self.events.append(
            _Event("show_plan", {"items": [(i.name, i.status) for i in items]})
        )

    async def append_plan(self, items: list[PlanItem]) -> None:
        self.events.append(
            _Event("append_plan", {"items": [(i.name, i.status) for i in items]})
        )

    async def tool_started(self, index: int, item: PlanItem) -> None:
        self.events.append(
            _Event("tool_started", {"index": index, "name": item.name})
        )

    async def tool_finished(self, index: int, item: PlanItem) -> None:
        self.events.append(
            _Event(
                "tool_finished",
                {
                    "index": index,
                    "name": item.name,
                    "status": item.status,
                    "duration_s": item.duration_s,
                },
            )
        )

    async def stream_token(self, token: str) -> None:
        self.events.append(_Event("stream_token", {"token": token}))

    async def stream_end(self, full_text: str) -> None:
        self.events.append(_Event("stream_end", {"full_text": full_text}))

    async def thinking_update(self, text: str) -> None:
        self.events.append(_Event("thinking_update", {"text": text}))


# ---------- fake SDK blocks ------------------------------------------


@dataclass
class _FakeTextBlock:
    text: str


@dataclass
class _FakeToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class _FakeThinkingBlock:
    thinking: str
    signature: str = ""


# ---------- tests: _clean_tool_name / _format_tool_sig ---------------


def test_clean_tool_name() -> None:
    assert _clean_tool_name("mcp__imp_foreman__list_issues") == "list_issues"
    assert _clean_tool_name("list_issues") == "list_issues"
    assert _clean_tool_name("mcp__other__foo") == "mcp__other__foo"
    print("  ✓ _clean_tool_name")


def test_format_tool_sig() -> None:
    assert _format_tool_sig("list_issues", {}) == "`list_issues()`"
    assert (
        _format_tool_sig("view_issue", {"number": 42})
        == '`view_issue(number=42)`'
    )
    assert (
        _format_tool_sig("list_issues", {"state": "open", "limit": 10})
        == '`list_issues(state="open", limit=10)`'
    )
    print("  ✓ _format_tool_sig")


# ---------- tests: _ToolTracker --------------------------------------


def test_tracker_register_batch() -> None:
    ui = RecordingUI()
    tracker = _ToolTracker(ui)

    blocks = [
        _FakeToolUseBlock("tu_1", "mcp__imp_foreman__list_issues", {"state": "open"}),
        _FakeToolUseBlock("tu_2", "mcp__imp_foreman__view_issue", {"number": 42}),
    ]
    new = tracker.register_batch(blocks)
    assert len(new) == 2
    assert new[0].name == "list_issues"
    assert new[1].name == "view_issue"
    assert len(tracker.plan_items) == 2
    print("  ✓ tracker.register_batch")


def test_tracker_on_start_on_done() -> None:
    ui = RecordingUI()
    tracker = _ToolTracker(ui)

    blocks = [
        _FakeToolUseBlock("tu_1", "mcp__imp_foreman__list_issues", {"state": "open"}),
        _FakeToolUseBlock("tu_2", "mcp__imp_foreman__view_issue", {"number": 42}),
    ]
    tracker.register_batch(blocks)

    async def _run() -> None:
        await tracker._on_start("list_issues")
        assert tracker.plan_items[0].status == "running"

        await tracker._on_done("list_issues", True, 0.5, '{"issues": []}')
        assert tracker.plan_items[0].status == "ok"
        assert tracker.plan_items[0].duration_s == 0.5

        await tracker._on_start("view_issue")
        await tracker._on_done("view_issue", False, 1.2, "not found")
        assert tracker.plan_items[1].status == "error"

    asyncio.run(_run())

    kinds = [e.kind for e in ui.events]
    assert kinds == [
        "tool_started",
        "tool_finished",
        "tool_started",
        "tool_finished",
    ]
    # First tool: ok
    assert ui.events[1].args["status"] == "ok"
    # Second tool: error
    assert ui.events[3].args["status"] == "error"
    print("  ✓ tracker._on_start / _on_done")


def test_tracker_duplicate_tool_name() -> None:
    """Two calls to the same tool in one batch resolve in order."""
    ui = RecordingUI()
    tracker = _ToolTracker(ui)

    blocks = [
        _FakeToolUseBlock("tu_1", "mcp__imp_foreman__view_issue", {"number": 1}),
        _FakeToolUseBlock("tu_2", "mcp__imp_foreman__view_issue", {"number": 2}),
    ]
    tracker.register_batch(blocks)

    async def _run() -> None:
        await tracker._on_start("view_issue")
        await tracker._on_done("view_issue", True, 0.3, "ok1")
        assert tracker.plan_items[0].status == "ok"
        assert tracker.plan_items[1].status == "pending"

        await tracker._on_start("view_issue")
        await tracker._on_done("view_issue", True, 0.4, "ok2")
        assert tracker.plan_items[1].status == "ok"

    asyncio.run(_run())
    assert ui.events[0].args["index"] == 0
    assert ui.events[2].args["index"] == 1
    print("  ✓ tracker: duplicate tool names resolve in order")


# ---------- tests: multi-wave plan -----------------------------------


def test_tracker_multi_wave() -> None:
    """Follow-up tool calls append to the plan."""
    ui = RecordingUI()
    tracker = _ToolTracker(ui)

    # Wave 1
    batch1 = [_FakeToolUseBlock("tu_1", "mcp__imp_foreman__list_issues", {})]
    tracker.register_batch(batch1)
    assert len(tracker.plan_items) == 1

    # Wave 2
    batch2 = [_FakeToolUseBlock("tu_2", "mcp__imp_foreman__view_issue", {"number": 5})]
    tracker.register_batch(batch2)
    assert len(tracker.plan_items) == 2
    assert tracker.plan_items[1].name == "view_issue"
    print("  ✓ tracker: multi-wave append")


# ---------- tests: zero-tool turn ------------------------------------


def test_zero_tools_text_only() -> None:
    """Text-only turn: stream_token for each TextBlock, stream_end."""
    ui = RecordingUI()

    async def _run() -> None:
        await ui.stream_token("Hello ")
        await ui.stream_token("world")
        await ui.stream_end("Hello world")

    asyncio.run(_run())

    kinds = [e.kind for e in ui.events]
    assert kinds == ["stream_token", "stream_token", "stream_end"]
    assert ui.events[2].args["full_text"] == "Hello world"
    print("  ✓ zero-tool turn: text-only streaming")


# ---------- tests: thinking blocks -----------------------------------


def test_thinking_blocks() -> None:
    """Thinking blocks trigger thinking_update."""
    ui = RecordingUI()

    async def _run() -> None:
        await ui.thinking_update("I should list the issues first.")
        await ui.thinking_update("Then check issue 42.")

    asyncio.run(_run())

    assert len(ui.events) == 2
    assert ui.events[0].kind == "thinking_update"
    assert "list the issues" in ui.events[0].args["text"]
    print("  ✓ thinking blocks")


# ---------- tests: full scenario — tool batch then text ---------------


def test_full_scenario_tools_then_text() -> None:
    """Simulate: thinking → tool batch → tool execution → text stream."""
    ui = RecordingUI()
    tracker = _ToolTracker(ui)

    blocks_wave1 = [
        _FakeToolUseBlock("tu_1", "mcp__imp_foreman__list_issues", {"state": "open"}),
        _FakeToolUseBlock("tu_2", "mcp__imp_foreman__view_issue", {"number": 42}),
    ]

    async def _run() -> None:
        # 1. Thinking
        await ui.thinking_update("Let me check the issues.")

        # 2. Show plan
        tracker.register_batch(blocks_wave1)
        await ui.show_plan(tracker.plan_items)

        # 3. Tools execute
        await tracker._on_start("list_issues")
        await tracker._on_done("list_issues", True, 0.8, '{"issues": [1,2]}')
        await tracker._on_start("view_issue")
        await tracker._on_done("view_issue", True, 0.3, '{"title": "bug"}')

        # 4. Stream text
        await ui.stream_token("Based on the issues, ")
        await ui.stream_token("here is my analysis.")
        await ui.stream_end("Based on the issues, here is my analysis.")

    asyncio.run(_run())

    kinds = [e.kind for e in ui.events]
    assert kinds == [
        "thinking_update",
        "show_plan",
        "tool_started",
        "tool_finished",
        "tool_started",
        "tool_finished",
        "stream_token",
        "stream_token",
        "stream_end",
    ]
    print("  ✓ full scenario: tools → text")


# ---------- tests: interleaved tool batches ---------------------------


def test_interleaved_tool_batches() -> None:
    """Two waves of tool calls with text in between."""
    ui = RecordingUI()
    tracker = _ToolTracker(ui)

    async def _run() -> None:
        # Wave 1
        batch1 = [
            _FakeToolUseBlock("tu_1", "mcp__imp_foreman__list_issues", {}),
        ]
        tracker.register_batch(batch1)
        await ui.show_plan(tracker.plan_items)

        await tracker._on_start("list_issues")
        await tracker._on_done("list_issues", True, 0.5, "ok")

        await ui.stream_token("Found issues. ")

        # Wave 2
        batch2 = [
            _FakeToolUseBlock("tu_2", "mcp__imp_foreman__view_issue", {"number": 1}),
        ]
        new_items = tracker.register_batch(batch2)
        await ui.append_plan(new_items)

        await tracker._on_start("view_issue")
        await tracker._on_done("view_issue", True, 0.3, "ok")

        await ui.stream_token("Done.")
        await ui.stream_end("Found issues. Done.")

    asyncio.run(_run())

    kinds = [e.kind for e in ui.events]
    assert "show_plan" in kinds
    assert "append_plan" in kinds
    assert kinds.count("tool_started") == 2
    assert kinds.count("tool_finished") == 2
    assert kinds.count("stream_token") == 2
    assert "stream_end" in kinds
    print("  ✓ interleaved tool batches")


# ---------- tests: tool failure ---------------------------------------


def test_tool_failure() -> None:
    """A tool that errors shows status='error'."""
    ui = RecordingUI()
    tracker = _ToolTracker(ui)

    blocks = [
        _FakeToolUseBlock("tu_1", "mcp__imp_foreman__run_shell", {"argv": ["false"]}),
    ]
    tracker.register_batch(blocks)

    async def _run() -> None:
        await tracker._on_start("run_shell")
        await tracker._on_done("run_shell", False, 0.1, "exit code 1")

    asyncio.run(_run())

    assert tracker.plan_items[0].status == "error"
    assert ui.events[1].args["status"] == "error"
    print("  ✓ tool failure")


# ---------- tests: no thinking blocks --------------------------------


def test_no_thinking_blocks() -> None:
    """Turn with tools but no thinking blocks — thinking_update never called."""
    ui = RecordingUI()
    tracker = _ToolTracker(ui)

    blocks = [_FakeToolUseBlock("tu_1", "mcp__imp_foreman__get_budgets", {})]
    tracker.register_batch(blocks)

    async def _run() -> None:
        await ui.show_plan(tracker.plan_items)
        await tracker._on_start("get_budgets")
        await tracker._on_done("get_budgets", True, 0.1, "{}")
        await ui.stream_token("Budgets are fine.")
        await ui.stream_end("Budgets are fine.")

    asyncio.run(_run())

    kinds = [e.kind for e in ui.events]
    assert "thinking_update" not in kinds
    print("  ✓ no thinking blocks")


# ---------- runner ----------------------------------------------------


def main() -> None:
    print("test_foreman_turn_ui")

    test_clean_tool_name()
    test_format_tool_sig()
    test_tracker_register_batch()
    test_tracker_on_start_on_done()
    test_tracker_duplicate_tool_name()
    test_tracker_multi_wave()
    test_zero_tools_text_only()
    test_thinking_blocks()
    test_full_scenario_tools_then_text()
    test_interleaved_tool_batches()
    test_tool_failure()
    test_no_thinking_blocks()

    print(f"\nAll {12} tests passed.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        sys.exit(1)
