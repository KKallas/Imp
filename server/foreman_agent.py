"""server/foreman_agent.py — thin bridge to Claude Code.

Claude Code runs natively with its own tools (Bash, Read, Write, etc.).
Security is enforced via a ``can_use_tool`` hook that routes every
tool use through ``intercept.py`` (whitelist + classify) and
``guard.py`` (LLM approval for writes) with budget tracking.

No MCP server. No hardcoded tool functions. The agent is just:
  1. Load system prompt
  2. Set up security hook
  3. Run the SDK streaming loop
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from . import budgets, guard, intercept

ROOT = Path(__file__).resolve().parent.parent
_PROMPT_FILE = ROOT / "server" / "foreman_prompt.md"

# Re-export for backward compat (chat_ws.py, main.py import these)
from .turn_ui import (  # noqa: F401, E402
    PlanItem,
    TurnUI,
    ToolTracker as _ToolTracker,
    clean_tool_name as _clean_tool_name,
    format_tool_sig as _format_tool_sig,
)


# ---------- system prompt ----------


def _load_system_prompt() -> str:
    """Load from file at dispatch time — hot-reloadable, editable."""
    if _PROMPT_FILE.exists():
        return _PROMPT_FILE.read_text()
    return "You are Foreman, an AI project manager. Use shell commands via gh CLI."


# ---------- security hook ----------


async def _security_hook(
    tool_name: str,
    tool_input: dict[str, Any],
    context: Any,
) -> Any:
    """Called by Claude SDK before every tool use.

    Routes Bash commands through intercept (whitelist + classify) and
    guard (LLM approval for writes). Other tools (Read, Write, Grep,
    etc.) are allowed — they operate on local files only.
    """
    from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny

    # Non-Bash tools are safe (local file operations)
    if tool_name != "Bash":
        return PermissionResultAllow(behavior="allow")

    command = tool_input.get("command", "")
    if not command.strip():
        return PermissionResultAllow(behavior="allow")

    # Parse the command into argv for intercept
    try:
        argv = shlex.split(command)
    except ValueError:
        argv = command.split()

    if not argv:
        return PermissionResultAllow(behavior="allow")

    # Classify the command
    classification = intercept.classify_command(argv)

    if classification == "unknown":
        return PermissionResultDeny(
            behavior="deny",
            message=f"Command not recognized by security policy: {argv[0]}",
            interrupt=False,
        )

    # Writes go through the guard
    if classification == "write":
        ok, reason = await guard.check_action(
            user_intent="",  # filled by dispatch context
            proposed_command=command,
            worker_rationale="Foreman agent tool use",
        )
        if not ok:
            return PermissionResultDeny(
                behavior="deny",
                message=f"Guard rejected: {reason}",
                interrupt=False,
            )

        # Budget check
        b = budgets.get_budgets()
        if b.any_exhausted():
            exhausted = [c for c in ("tokens", "edits", "tasks") if b.exhausted(c)]
            return PermissionResultDeny(
                behavior="deny",
                message=f"Budget exhausted: {', '.join(exhausted)}",
                interrupt=False,
            )

    return PermissionResultAllow(behavior="allow")


# ---------- dispatch ----------


SayFn = Callable[[str], Awaitable[None]]
AskFn = Callable[[str], Awaitable[Optional[str]]]
ThinkingFn = Callable[[str], Any]
ChartFn = Callable[[dict[str, Any]], Awaitable[None]]


class _NullAsyncContext:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None


async def dispatch(
    user_text: str,
    *,
    say: SayFn,
    ask: AskFn,
    thinking: Optional[ThinkingFn] = None,
    chart: Optional[ChartFn] = None,
    history: Optional[list[Any]] = None,
    turn_ui: Optional[TurnUI] = None,
) -> str:
    """Run one Foreman conversation turn.

    Claude Code runs with native tools (Bash, Read, Write, etc.).
    The ``_security_hook`` enforces guard + intercept + budgets on
    every tool use. No MCP server needed.
    """
    from server import chat_history

    print(f"[foreman] dispatch: {user_text!r}", file=sys.stderr)

    preamble = chat_history.history_preamble(history or [])
    prompt_text = preamble + user_text if preamble else user_text

    from claude_agent_sdk import (  # type: ignore[import-not-found]
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKClient,
        ResultMessage,
        TextBlock,
        ThinkingBlock,
        ToolUseBlock,
    )

    ui = turn_ui or TurnUI()
    tracker = _ToolTracker(ui) if turn_ui is not None else None

    options = ClaudeAgentOptions(
        system_prompt=_load_system_prompt(),
        can_use_tool=_security_hook,
        max_turns=20,
        thinking={"type": "enabled", "budget_tokens": 10000},
    )

    cm_factory = thinking if thinking is not None else (lambda _label: _NullAsyncContext())

    assistant_chunks: list[str] = []
    tool_calls_seen: list[str] = []
    has_plan = False

    try:
        async with cm_factory("Foreman"):
            async with ClaudeSDKClient(options=options) as client:
                await client.query(prompt_text)
                async for message in client.receive_response():
                    if isinstance(message, AssistantMessage):
                        msg_thinking: list[Any] = []
                        msg_tools: list[Any] = []
                        msg_text: list[Any] = []
                        for block in message.content:
                            if isinstance(block, ThinkingBlock):
                                msg_thinking.append(block)
                            elif isinstance(block, ToolUseBlock):
                                tool_calls_seen.append(block.name)
                                msg_tools.append(block)
                            elif isinstance(block, TextBlock):
                                msg_text.append(block)

                        for b in msg_thinking:
                            await ui.thinking_update(b.thinking)

                        if msg_tools and tracker is not None:
                            new_items = tracker.register_batch(msg_tools)
                            if not has_plan:
                                await ui.show_plan(tracker.plan_items)
                                has_plan = True
                            else:
                                await ui.append_plan(new_items)

                        for b in msg_text:
                            assistant_chunks.append(b.text)
                            if not msg_tools:
                                await ui.stream_token(b.text)

                    elif isinstance(message, ResultMessage):
                        usage = getattr(message, "usage", None) or {}
                        in_tok = int(usage.get("input_tokens", 0) or 0)
                        out_tok = int(usage.get("output_tokens", 0) or 0)
                        if in_tok > 0 or out_tok > 0:
                            budgets.add_tokens(in_tok, out_tok)

    except Exception as exc:  # noqa: BLE001
        print(f"[foreman] backend error: {type(exc).__name__}: {exc}", file=sys.stderr)
        await say(f"Foreman backend error: {exc}")
        return ""

    reply = "".join(assistant_chunks).strip()

    if turn_ui is not None:
        if reply:
            await ui.stream_end(reply)
        elif tool_calls_seen:
            await say(
                f"_(Foreman used {len(tool_calls_seen)} tool call(s) "
                f"but produced no prose reply.)_"
            )
    else:
        if reply:
            await say(reply)

    return reply
