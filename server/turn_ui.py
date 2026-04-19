"""server/turn_ui.py — structured turn UI types.

Extracted from foreman_agent.py so the agent stays UI-agnostic.
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
    """Callback interface for structured tool-call rendering."""

    async def show_plan(self, items: list[PlanItem]) -> None: ...
    async def append_plan(self, items: list[PlanItem]) -> None: ...
    async def tool_started(self, index: int, item: PlanItem) -> None: ...
    async def tool_finished(self, index: int, item: PlanItem) -> None: ...
    async def stream_token(self, token: str) -> None: ...
    async def stream_end(self, full_text: str) -> None: ...
    async def thinking_update(self, text: str) -> None: ...


class ToolTracker:
    """Wraps tool handlers to emit per-tool start/finish events."""

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
