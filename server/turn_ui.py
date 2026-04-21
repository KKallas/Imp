"""server/turn_ui.py — per-event callback interface for agent turn rendering.

The agent dispatch loop in foreman_agent.py fires callbacks on a TurnUI
implementation at specific moments during a turn. A ToolTracker sits in
between, translating raw tool-use blocks into structured PlanItem lifecycle
events.

Typical call sequence within a single turn::

    thinking_update
    show_plan          (first batch of tool calls discovered)
    tool_started       ─┐
    tool_finished       │  repeated per tool in the batch
    tool_started        │
    tool_finished      ─┘
    append_plan        (if the model issues more tool calls)
    tool_started / tool_finished …
    stream_token       ─┐
    stream_token        │  repeated per chunk of the reply
    stream_token       ─┘
    stream_end

Implementors: WebSocketTurnUI (chat_ws.py), RecordingUI (tests).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

def clean_tool_name(name: str) -> str:
    """Return the tool name as-is (no prefix stripping needed)."""
    return name


def format_tool_sig(name: str, args: dict[str, Any]) -> str:
    """Format a tool call as a readable function signature."""
    if not args:
        return f"`{name}()`"
    parts = []
    for k, v in args.items():
        if isinstance(v, str):
            parts.append(f'{k}="{v}"')
        else:
            parts.append(f"{k}={v}")
    return f"`{name}({', '.join(parts)})`"


@dataclass
class PlanItem:
    """One tool call in a turn's plan checklist."""
    name: str
    args: dict[str, Any]
    status: str = "pending"
    duration_s: float = 0.0
    output: str = ""


class TurnUI:
    """Per-event callback interface for rendering an agent turn.

    Each method fires once per event during the turn lifecycle.
    Subclass and override to push events to a specific transport
    (e.g. WebSocket, test recorder).  The default implementations
    are no-ops so consumers can override only what they need.
    """

    async def show_plan(self, items: list[PlanItem]) -> None:
        """Called once when the first batch of tool calls is known."""
        ...

    async def append_plan(self, items: list[PlanItem]) -> None:
        """Called when additional tool calls are discovered mid-turn."""
        ...

    async def tool_started(self, index: int, item: PlanItem) -> None:
        """Called once per tool, right before execution begins."""
        ...

    async def tool_finished(self, index: int, item: PlanItem) -> None:
        """Called once per tool, after execution completes (ok or error)."""
        ...

    async def stream_token(self, token: str) -> None:
        """Called for each text chunk as the model streams its reply."""
        ...

    async def stream_end(self, full_text: str) -> None:
        """Called once when the full reply text has been assembled."""
        ...

    async def thinking_update(self, text: str) -> None:
        """Called once per thinking block emitted by the model."""
        ...


class ToolTracker:
    """Bridge between the agent dispatch loop and a TurnUI.

    The dispatch loop in foreman_agent.py hands raw tool-use blocks to
    register_batch(), which converts them into PlanItems. As each tool
    executes, on_start() and on_done() fire the corresponding
    tool_started / tool_finished callbacks on the TurnUI.
    """

    def __init__(self, turn_ui: TurnUI) -> None:
        self.turn_ui = turn_ui
        self.plan_items: list[PlanItem] = []
        self._pending: dict[str, list[int]] = {}

    def register_batch(self, tool_blocks: list[Any]) -> list[PlanItem]:
        new_items: list[PlanItem] = []
        for block in tool_blocks:
            clean = clean_tool_name(block.name)
            item = PlanItem(name=clean, args=block.input or {})
            idx = len(self.plan_items)
            self.plan_items.append(item)
            self._pending.setdefault(clean, []).append(idx)
            new_items.append(item)
        return new_items

    async def on_start(self, tool_name: str) -> None:
        indices = self._pending.get(tool_name, [])
        if not indices:
            return
        idx = indices[0]
        self.plan_items[idx].status = "running"
        await self.turn_ui.tool_started(idx, self.plan_items[idx])

    async def on_done(
        self, tool_name: str, ok: bool, duration: float, output: str
    ) -> None:
        indices = self._pending.get(tool_name, [])
        if not indices:
            return
        idx = indices.pop(0)
        item = self.plan_items[idx]
        item.status = "ok" if ok else "error"
        item.duration_s = duration
        item.output = output
        await self.turn_ui.tool_finished(idx, item)
