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
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from . import budgets, guard

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


_cached_prompt: str | None = None


def _load_system_prompt() -> str:
    """Return cached system prompt. Built once at first call."""
    global _cached_prompt
    if _cached_prompt is not None:
        return _cached_prompt
    _cached_prompt = _build_system_prompt()
    return _cached_prompt


def _build_system_prompt() -> str:
    """Load from file + append auto-discovered tool list."""
    base = ""
    if _PROMPT_FILE.exists():
        base = _PROMPT_FILE.read_text()
    else:
        base = "You are Foreman, an AI project manager."

    # Append available tools so Claude tries them before raw Bash
    try:
        import tools
        tool_list = tools.build_tool_list_for_prompt()
        if tool_list:
            base += "\n\n" + tool_list
    except Exception:
        pass

    return base


def reload_prompt() -> str:
    """Force-rebuild and re-cache the system prompt. Called by reload_tools.py."""
    global _cached_prompt
    _cached_prompt = _build_system_prompt()
    return _cached_prompt


# ---------- security hook ----------

# Read-only tools that never need confirmation
_SAFE_TOOLS = {"Read", "Glob", "Grep", "LS", "View"}

ConfirmFn = Callable[[str, str, str], Awaitable[bool]]


def _make_security_hook(confirm: Optional[ConfirmFn] = None):
    """Create a security hook closure that can request user confirmation.

    confirm(tool, description, preview) -> bool
    """
    async def hook(tool_name: str, tool_input: dict[str, Any], context: Any) -> Any:
        from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny

        # Read-only tools are always safe
        if tool_name in _SAFE_TOOLS:
            return PermissionResultAllow(behavior="allow")

        # Write/Edit tool — show diff preview
        if tool_name == "Write" or tool_name == "Edit":
            if confirm:
                file_path = tool_input.get("file_path", "")
                if tool_name == "Edit":
                    old = tool_input.get("old_string", "")
                    new = tool_input.get("new_string", "")
                    diff_lines = []
                    for line in old.splitlines():
                        diff_lines.append(f"- {line}")
                    for line in new.splitlines():
                        diff_lines.append(f"+ {line}")
                    preview = "\n".join(diff_lines)
                    desc = f"Edit {file_path}"
                else:
                    content = tool_input.get("content", "")
                    preview = "\n".join(f"+ {line}" for line in content[:2000].splitlines())
                    desc = f"Write {file_path} ({len(content)} chars)"
                approved = await confirm(tool_name, desc, preview)
                if not approved:
                    return PermissionResultDeny(behavior="deny", message="User rejected", interrupt=False)
            return PermissionResultAllow(behavior="allow")

        # Bash tool — show command
        if tool_name == "Bash":
            command = tool_input.get("command", "")
            if not command.strip():
                return PermissionResultAllow(behavior="allow")

            # Budget check
            b = budgets.get_budgets()
            if b.any_exhausted():
                exhausted = [c for c in ("tokens", "edits", "tasks") if b.exhausted(c)]
                return PermissionResultDeny(behavior="deny", message=f"Budget exhausted: {', '.join(exhausted)}", interrupt=False)

            # Classify
            try:
                argv = shlex.split(command)
            except ValueError:
                argv = command.split()

            classification = guard.classify_command(argv) if argv else "read"

            # Reads don't need confirmation
            if classification == "read":
                return PermissionResultAllow(behavior="allow")

            # Writes need user confirmation
            if confirm:
                desc = tool_input.get("description", command[:80])
                approved = await confirm(tool_name, desc, command)
                if not approved:
                    return PermissionResultDeny(behavior="deny", message="User rejected", interrupt=False)
            return PermissionResultAllow(behavior="allow")

        # Other tools — ask for confirmation if available
        if confirm:
            desc = tool_input.get("description", tool_name)
            preview = str(tool_input)[:1000]
            approved = await confirm(tool_name, desc, preview)
            if not approved:
                return PermissionResultDeny(behavior="deny", message="User rejected", interrupt=False)
        return PermissionResultAllow(behavior="allow")

    return hook


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
    confirm: Optional[ConfirmFn] = None,
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
        UserMessage,
    )
    from claude_agent_sdk.types import ToolResultBlock

    ui = turn_ui or TurnUI()
    tracker = _ToolTracker(ui) if turn_ui is not None else None

    options = ClaudeAgentOptions(
        system_prompt=_load_system_prompt(),
        can_use_tool=_make_security_hook(confirm),
        max_turns=20,
        thinking={"type": "enabled", "budget_tokens": 10000},
    )

    cm_factory = thinking if thinking is not None else (lambda _label: _NullAsyncContext())

    assistant_chunks: list[str] = []
    tool_calls_seen: list[str] = []
    has_plan = False
    # Track pending tool calls by ID so we can match results
    _pending_tool_ids: dict[str, tuple[str, float]] = {}  # id → (name, start_time)

    try:
        async with cm_factory("Foreman"):
            async with ClaudeSDKClient(options=options) as client:
                await client.query(prompt_text)
                async for message in client.receive_response():
                    print(
                        f"[foreman] msg: {type(message).__name__} "
                        f"blocks={[type(b).__name__ for b in getattr(message, 'content', [])]}"
                        if hasattr(message, 'content') else
                        f"[foreman] msg: {type(message).__name__}",
                        file=sys.stderr,
                    )
                    if isinstance(message, AssistantMessage):
                        msg_thinking: list[Any] = []
                        msg_tools: list[Any] = []
                        msg_results: list[Any] = []
                        msg_text: list[Any] = []
                        for block in message.content:
                            if isinstance(block, ThinkingBlock):
                                msg_thinking.append(block)
                            elif isinstance(block, ToolUseBlock):
                                tool_calls_seen.append(block.name)
                                msg_tools.append(block)
                            elif isinstance(block, ToolResultBlock):
                                msg_results.append(block)
                            elif isinstance(block, TextBlock):
                                msg_text.append(block)
                            else:
                                # Log unknown block types for debugging
                                print(
                                    f"[foreman] unknown block: {type(block).__name__}",
                                    file=sys.stderr,
                                )

                        for b in msg_thinking:
                            await ui.thinking_update(b.thinking)

                        # Register new tool calls in the tracker
                        if msg_tools and tracker is not None:
                            for block in msg_tools:
                                cmd = (block.input or {}).get('command', '')[:80]
                                desc = (block.input or {}).get('description', '')
                                print(f"[TRACE] ToolUseBlock id={block.id} name={_clean_tool_name(block.name)} cmd={cmd!r} desc={desc!r}", file=sys.stderr)
                            new_items = tracker.register_batch(msg_tools)
                            if not has_plan:
                                await ui.show_plan(tracker.plan_items)
                                has_plan = True
                            else:
                                await ui.append_plan(new_items)
                            for block in msg_tools:
                                _pending_tool_ids[block.id] = (
                                    _clean_tool_name(block.name),
                                    time.monotonic(),
                                )
                                print(f"[TRACE] on_start id={block.id} name={_clean_tool_name(block.name)} pending_count={len(_pending_tool_ids)}", file=sys.stderr)
                                await tracker.on_start(
                                    _clean_tool_name(block.name)
                                )

                        # Match tool results to their calls (same message)
                        for result_block in msg_results:
                            print(f"[TRACE] ToolResultBlock(AssistantMsg) tool_use_id={result_block.tool_use_id} found={result_block.tool_use_id in _pending_tool_ids}", file=sys.stderr)
                            entry = _pending_tool_ids.pop(
                                result_block.tool_use_id, None
                            )
                            if entry and tracker is not None:
                                tool_name, start_t = entry
                                duration = time.monotonic() - start_t
                                output = ""
                                if isinstance(result_block.content, str):
                                    output = result_block.content
                                elif isinstance(result_block.content, list):
                                    output = str(result_block.content)
                                ok = not result_block.is_error
                                print(f"[TRACE] on_done(AssistantMsg) name={tool_name} ok={ok}", file=sys.stderr)
                                await tracker.on_done(
                                    tool_name, ok, duration, output[:4000]
                                )

                        for b in msg_text:
                            assistant_chunks.append(b.text)
                            if not msg_tools:
                                await ui.stream_token(b.text)

                    elif isinstance(message, UserMessage):
                        for block in message.content:
                            if isinstance(block, ToolResultBlock):
                                print(f"[TRACE] ToolResultBlock(UserMsg) tool_use_id={block.tool_use_id} found={block.tool_use_id in _pending_tool_ids}", file=sys.stderr)
                                entry = _pending_tool_ids.pop(
                                    block.tool_use_id, None
                                )
                                if entry and tracker is not None:
                                    tool_name, start_t = entry
                                    duration = time.monotonic() - start_t
                                    output = ""
                                    if isinstance(block.content, str):
                                        output = block.content
                                    elif isinstance(block.content, list):
                                        output = str(block.content)
                                    ok = not block.is_error
                                    print(f"[TRACE] on_done(UserMsg) name={tool_name} ok={ok}", file=sys.stderr)
                                    await tracker.on_done(
                                        tool_name, ok, duration, output[:4000]
                                    )

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
