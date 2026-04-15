"""server/foreman_agent.py — Foreman, the post-setup worker agent.

Loaded by main.py once `setup_complete=true`. Replaces the P2.9
JSON-verdict dispatcher with a real `claude-agent-sdk` tool-using agent
so the LLM can chain commands, interpret their output, and answer
questions from tool results (the gap KKallas/Imp#36 worked around).

## Architecture

Foreman uses `ClaudeSDKClient` for multi-turn conversation + native
`@tool`-decorated Python functions. **The built-in `Bash` tool is
explicitly disallowed**: every shell invocation MUST route through
`intercept.execute_command` so the guard + three budgets still fire.
That routing is enforced by construction — the only path from the LLM
to a subprocess is via our MCP tools, each of which is a thin wrapper
over `intercept.execute_command`. There's no Bash permission to bypass.

This is a stronger guarantee than a `PreToolUseHook` gate (which could
only approve/deny a Bash call — it can't substitute the execution) and
matches the same "no tools" pattern used by `server/guard.py` and
`server/dispatcher.py`.

## Tool registry (per v0.1.md §The Agent's Role)

Read / visibility — no guard checkpoint:
  - list_issues / view_issue / list_prs / view_pr
  - list_project_items
  - run_sync / run_heuristics / run_render_chart / run_scenario (pipeline
    scripts; P4.12–16 replace the stub bodies)

PM writes — gated by Guard checkpoint B via intercept:
  - comment_on_issue, edit_issue, close_issue, reopen_issue,
    edit_project_field, create_issue, add_issue_to_project

Code-writing pipeline — gated by Guard + task/token budgets:
  - run_moderate_issues, run_solve_issues, run_fix_prs, run_all_tools

Chat / control (local-only, no guard):
  - loop_pause / loop_resume / loop_scope / loop_clear_scope
  - get_budgets (read-only — budget setters are admin-UI only, not
    agent-callable per the P2.8 design decision)

Escape hatch:
  - run_shell(argv) — raw argv through intercept, for anything not
    covered by a named tool. Guard classifies and enforces per usual.

## No chainlit import

Module has no chainlit imports; UI seams (`say`, `ask`, `thinking`)
are passed in as callables. main.py wires them to `cl.Message`,
`cl.AskUserMessage`, and `cl.Step(type="run")`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from . import budgets, intercept

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / ".imp" / "config.json"
OUTPUT_DIR = ROOT / ".imp" / "output"

# Module-level artifact collector — populated by tool bodies that
# produce chat-renderable side outputs (currently: chart Plotly JSONs).
# `dispatch()` clears this at the start of every turn and reads it at
# the end so it can pass artifact descriptors to the UI layer via the
# `chart` callable. Tests reset it via `_pending_artifacts.clear()`.
_pending_artifacts: list[dict[str, Any]] = []


# ---------- config I/O (local copy — server.setup_agent has another
# copy; if a third caller appears, lift into server/config.py) ----------


def _load_config() -> dict[str, Any]:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def _save_config(cfg: dict[str, Any]) -> None:
    CONFIG_FILE.parent.mkdir(exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


# ---------- tool bodies (do_*) — pure async functions, unit-testable ----------
#
# Every subprocess call flows through `intercept.execute_command`. The
# bodies capture `user_intent` from the closure in `_build_mcp_server()`
# below so the guard (checkpoint B) can compare proposed writes against
# what the admin actually asked for this turn.


async def do_run_shell(
    argv: list[str],
    *,
    user_intent: str,
    rationale: str,
) -> dict[str, Any]:
    """General-purpose shell escape hatch. Any command the classifier
    recognises (gh *, pipeline scripts, demo-safe shell) can run here."""
    rc, output, action = await intercept.execute_command(
        argv,
        user_intent=user_intent,
        rationale=rationale,
        kind="foreman",
    )
    return _shell_result(rc, output, action)


def _shell_result(
    rc: int, output: str, action: intercept.ProposedAction
) -> dict[str, Any]:
    """Normalise `intercept.execute_command` returns for tool output.

    Caps `output` at 8k chars so a chatty subprocess can't blow out the
    LLM's context. Full output still on disk at `.imp/output/<id>.log`.
    """
    trimmed = output.rstrip()
    if len(trimmed) > 8000:
        trimmed = trimmed[:8000] + "\n... [truncated — see .imp/output/<id>.log]"
    return {
        "exit_code": rc,
        "output": trimmed,
        "action_id": action.action_id,
        "verdict": action.verdict,
        "verdict_reason": action.verdict_reason,
        "classified_as": action.classified_as,
    }


# ---------- read / visibility ----------


async def do_list_issues(
    state: str = "open",
    limit: int = 30,
    *,
    user_intent: str,
) -> dict[str, Any]:
    argv = [
        "gh",
        "issue",
        "list",
        "--state",
        state,
        "--limit",
        str(limit),
    ]
    return await do_run_shell(
        argv, user_intent=user_intent, rationale=f"list issues (state={state})"
    )


async def do_view_issue(number: int, *, user_intent: str) -> dict[str, Any]:
    return await do_run_shell(
        ["gh", "issue", "view", str(number)],
        user_intent=user_intent,
        rationale=f"view issue #{number}",
    )


async def do_list_prs(
    state: str = "open",
    limit: int = 30,
    *,
    user_intent: str,
) -> dict[str, Any]:
    return await do_run_shell(
        ["gh", "pr", "list", "--state", state, "--limit", str(limit)],
        user_intent=user_intent,
        rationale=f"list PRs (state={state})",
    )


async def do_view_pr(number: int, *, user_intent: str) -> dict[str, Any]:
    return await do_run_shell(
        ["gh", "pr", "view", str(number)],
        user_intent=user_intent,
        rationale=f"view PR #{number}",
    )


async def do_list_project_items(
    project_number: int,
    owner: str,
    limit: int = 100,
    *,
    user_intent: str,
) -> dict[str, Any]:
    return await do_run_shell(
        [
            "gh",
            "project",
            "item-list",
            str(project_number),
            "--owner",
            owner,
            "--limit",
            str(limit),
            "--format",
            "json",
        ],
        user_intent=user_intent,
        rationale=f"list items on project #{project_number}",
    )


# ---------- PM writes (guard checkpoint B fires) ----------


async def do_comment_on_issue(
    number: int, body: str, *, user_intent: str
) -> dict[str, Any]:
    return await do_run_shell(
        ["gh", "issue", "comment", str(number), "--body", body],
        user_intent=user_intent,
        rationale=f"comment on issue #{number}",
    )


async def do_edit_issue(
    number: int,
    *,
    user_intent: str,
    add_labels: Optional[list[str]] = None,
    remove_labels: Optional[list[str]] = None,
    add_assignees: Optional[list[str]] = None,
    title: Optional[str] = None,
    milestone: Optional[str] = None,
) -> dict[str, Any]:
    argv: list[str] = ["gh", "issue", "edit", str(number)]
    if add_labels:
        for label in add_labels:
            argv.extend(["--add-label", label])
    if remove_labels:
        for label in remove_labels:
            argv.extend(["--remove-label", label])
    if add_assignees:
        for a in add_assignees:
            argv.extend(["--add-assignee", a])
    if title:
        argv.extend(["--title", title])
    if milestone:
        argv.extend(["--milestone", milestone])
    # If nothing to change, bail early with a clear message
    if len(argv) == 4:
        return {"error": "nothing to edit — provide at least one field"}
    return await do_run_shell(
        argv, user_intent=user_intent, rationale=f"edit issue #{number}"
    )


async def do_close_issue(
    number: int,
    *,
    user_intent: str,
    reason: Optional[str] = None,
    comment: Optional[str] = None,
) -> dict[str, Any]:
    argv = ["gh", "issue", "close", str(number)]
    if reason:
        argv.extend(["--reason", reason])
    if comment:
        argv.extend(["--comment", comment])
    return await do_run_shell(
        argv, user_intent=user_intent, rationale=f"close issue #{number}"
    )


async def do_reopen_issue(
    number: int, *, user_intent: str, comment: Optional[str] = None
) -> dict[str, Any]:
    argv = ["gh", "issue", "reopen", str(number)]
    if comment:
        argv.extend(["--comment", comment])
    return await do_run_shell(
        argv, user_intent=user_intent, rationale=f"reopen issue #{number}"
    )


async def do_create_issue(
    title: str,
    body: str,
    *,
    user_intent: str,
    labels: Optional[list[str]] = None,
    assignees: Optional[list[str]] = None,
) -> dict[str, Any]:
    argv = ["gh", "issue", "create", "--title", title, "--body", body]
    if labels:
        for label in labels:
            argv.extend(["--label", label])
    if assignees:
        for a in assignees:
            argv.extend(["--assignee", a])
    return await do_run_shell(
        argv, user_intent=user_intent, rationale=f"create issue: {title!r}"
    )


async def do_edit_project_field(
    project_number: int,
    owner: str,
    item_id: str,
    field_id: str,
    value: str,
    *,
    user_intent: str,
) -> dict[str, Any]:
    """Set a custom field on a project item.

    `field_id` / `item_id` are the GraphQL node IDs that `list_project_items`
    returns. The gh CLI expects them explicitly — names don't work here.
    """
    argv = [
        "gh",
        "project",
        "item-edit",
        "--project-id",
        str(project_number),
        "--owner",
        owner,
        "--id",
        item_id,
        "--field-id",
        field_id,
        "--text",
        value,
    ]
    return await do_run_shell(
        argv,
        user_intent=user_intent,
        rationale=f"set field {field_id!r}={value!r} on item {item_id!r}",
    )


# ---------- pipeline: code-writing (guard + task budget) ----------


async def do_run_moderate_issues(
    *,
    user_intent: str,
    issue: Optional[int] = None,
    max_tokens: Optional[int] = None,
) -> dict[str, Any]:
    argv = [sys.executable, "99-tools/moderate_issues.py"]
    if issue is not None:
        argv.extend(["--issue", str(issue)])
    if max_tokens is not None:
        argv.extend(["--max-tokens", str(max_tokens)])
    return await do_run_shell(
        argv, user_intent=user_intent, rationale="run moderation pipeline"
    )


async def do_run_solve_issues(
    *,
    user_intent: str,
    issue: int,
    max_tokens: Optional[int] = None,
) -> dict[str, Any]:
    argv = [sys.executable, "99-tools/solve_issues.py", "--issue", str(issue)]
    if max_tokens is not None:
        argv.extend(["--max-tokens", str(max_tokens)])
    return await do_run_shell(
        argv, user_intent=user_intent, rationale=f"solve issue #{issue}"
    )


async def do_run_fix_prs(
    *,
    user_intent: str,
    pr: int,
    max_tokens: Optional[int] = None,
) -> dict[str, Any]:
    argv = [sys.executable, "99-tools/fix_prs.py", "--pr", str(pr)]
    if max_tokens is not None:
        argv.extend(["--max-tokens", str(max_tokens)])
    return await do_run_shell(
        argv, user_intent=user_intent, rationale=f"fix PR #{pr}"
    )


# ---------- pipeline: read-side visibility scripts (stubs until P4.12–16) ----------


async def do_run_sync_issues(*, user_intent: str) -> dict[str, Any]:
    """pipeline/sync_issues.py — pulls issue state + project fields.
    Blocked on KKallas/Imp#12 (P4.12)."""
    argv = [sys.executable, "pipeline/sync_issues.py"]
    return await do_run_shell(
        argv, user_intent=user_intent, rationale="sync issues from GitHub"
    )


async def do_run_heuristics(*, user_intent: str) -> dict[str, Any]:
    """pipeline/heuristics.py — infers durations, dependencies, delays.
    Blocked on KKallas/Imp#13 (P4.13)."""
    argv = [sys.executable, "pipeline/heuristics.py"]
    return await do_run_shell(
        argv,
        user_intent=user_intent,
        rationale="run heuristic analysis on synced data",
    )


async def do_run_render_chart(
    template: str, *, user_intent: str
) -> dict[str, Any]:
    """Render a chart via `pipeline/render_chart.py`.

    On success, also picks up the `.plotly.json` the script writes
    alongside the HTML and pushes it into the module-level
    `_pending_artifacts` collector. `dispatch()` reads that after the
    turn and forwards it to the UI layer via the `chart` callable so
    the chart renders inline in chat (Chainlit 2.x doesn't render
    mermaid blocks; Plotly is the path that works today).
    """
    argv = [sys.executable, "pipeline/render_chart.py", "--template", template]
    result = await do_run_shell(
        argv, user_intent=user_intent, rationale=f"render {template} chart"
    )

    if result.get("exit_code") == 0 and result.get("verdict") != "reject":
        plotly_path = OUTPUT_DIR / f"{template}.plotly.json"
        if plotly_path.exists():
            _pending_artifacts.append(
                {
                    "type": "plotly",
                    "name": template,
                    "path": str(plotly_path),
                }
            )

    return result


async def do_run_scenario(
    delay: str, *, user_intent: str
) -> dict[str, Any]:
    """pipeline/scenario.py — A/B timeline comparison.
    Blocked on KKallas/Imp#16 (P4.16)."""
    argv = [sys.executable, "pipeline/scenario.py", "--delay", delay]
    return await do_run_shell(
        argv,
        user_intent=user_intent,
        rationale=f"scenario comparison with delay={delay!r}",
    )


# ---------- control tools (local, no guard) ----------


async def do_loop_pause() -> dict[str, Any]:
    cfg = _load_config()
    loop = cfg.setdefault("loop", {})
    loop["paused"] = True
    _save_config(cfg)
    return {"paused": True, "message": "Autonomous loop paused."}


async def do_loop_resume() -> dict[str, Any]:
    cfg = _load_config()
    loop = cfg.setdefault("loop", {})
    loop["paused"] = False
    _save_config(cfg)
    return {"paused": False, "message": "Autonomous loop resumed."}


async def do_loop_scope(
    only_issues: Optional[list[int]] = None,
    only_prs: Optional[list[int]] = None,
) -> dict[str, Any]:
    cfg = _load_config()
    loop = cfg.setdefault("loop", {})
    scope: dict[str, Any] = {}
    if only_issues:
        scope["only_issues"] = only_issues
    if only_prs:
        scope["only_prs"] = only_prs
    if not scope:
        return {"error": "provide at least one of only_issues, only_prs"}
    loop["scope"] = scope
    _save_config(cfg)
    return {"scope": scope, "message": f"Loop scoped to: {scope}"}


async def do_loop_clear_scope() -> dict[str, Any]:
    cfg = _load_config()
    loop = cfg.setdefault("loop", {})
    loop["scope"] = None
    _save_config(cfg)
    return {"scope": None, "message": "Loop scope cleared."}


async def do_get_budgets() -> dict[str, Any]:
    """Read-only budget status.

    Setters (`set_*_budget`, `reset_budgets`) are intentionally NOT
    exposed as agent tools per the P2.8 design: a budget the agent can
    lift isn't a budget. Admin changes them via the Chainlit gear-icon
    panel.
    """
    return budgets.get_budgets().to_dict()


# ---------- system prompt ----------

SYSTEM_PROMPT = """\
You are Foreman, an AI project manager and engineering assistant managing a \
GitHub repo for Imp — a self-hosted coding agent. You both **report on** the \
project (charts, status, delays) and **act on** it (triage issues, write \
code, open PRs, push fixes). You have two kinds of tools: read/visibility \
tools you can use freely, and write tools whose every invocation is reviewed \
by a separate Guard Agent before it actually executes.

## Core rules

- **Use the provided tools.** Never attempt shell commands directly — the \
built-in Bash tool is disallowed. Every shell invocation must go through \
one of our MCP tools, which route through `server/intercept.py` so the \
Guard Agent (checkpoint B) and the three budgets (tokens, edits, tasks) \
stay in enforcement.

- **Stay on the admin's stated intent.** If the admin said "moderate issue \
42," do NOT also drive-by update labels on other issues. The Guard Agent \
compares the exact command you propose against the admin's last message; \
off-intent writes are rejected.

- **Answer questions after running tools.** If the admin asks "how many \
issues are open?", call `list_issues`, then compose a plain prose answer \
from the output. Don't dump raw JSON when a sentence will do.

- **Stop when something fails.** If a tool returns a rejection from the \
guard or a budget-exhausted error, surface the reason and stop proposing \
more writes until the admin resolves it. Don't retry destructively.

## Tools available

### Read / visibility (free, no checkpoint)
- `list_issues(state, limit)` — `gh issue list`
- `view_issue(number)` — `gh issue view <n>`
- `list_prs(state, limit)` — `gh pr list`
- `view_pr(number)` — `gh pr view <n>`
- `list_project_items(project_number, owner)` — `gh project item-list`
- `run_sync_issues` / `run_heuristics` / `run_render_chart(template)` / \
`run_scenario(delay)` — pipeline visibility scripts (some still stubbed \
pending P4.12–16; they'll return a "script not found" error until then).

### PM writes (gated by checkpoint B, counts toward edit budget)
- `comment_on_issue(number, body)` — `gh issue comment`
- `edit_issue(number, add_labels, remove_labels, add_assignees, title, \
milestone)` — `gh issue edit`
- `close_issue(number, reason, comment)` / `reopen_issue(number, comment)`
- `create_issue(title, body, labels, assignees)`
- `edit_project_field(project_number, owner, item_id, field_id, value)`

### Code-writing pipeline (gated by checkpoint B + budgets)
- `run_moderate_issues(issue)` — one task off the task budget
- `run_solve_issues(issue)` — one task; writes a branch, opens a PR
- `run_fix_prs(pr)` — one task

### Control (local, no guard)
- `loop_pause` / `loop_resume` / `loop_scope(only_issues, only_prs)` / \
`loop_clear_scope`
- `get_budgets` — read-only. Admin changes limits via the gear-icon \
panel in the Chainlit UI; you cannot set or reset them.

### Escape hatch
- `run_shell(argv)` — any argv the classifier recognises. Prefer named \
tools when possible; fall back to this only when no named tool fits.

## How you respond

Plain markdown. When you call `run_render_chart`, the chart is \
attached to your reply automatically as an interactive Plotly figure \
— **don't paste mermaid or other chart syntax into your prose**, and \
don't embed a code fence with the chart text. Just describe what the \
chart shows in a sentence or two so the admin knows what they're \
looking at, then let the attached figure do the rest. Keep replies \
concise; the admin reads quickly.
"""


# ---------- MCP server factory ----------


def _build_mcp_server(user_intent: str) -> Any:
    """Build a fresh MCP server whose tool closures capture `user_intent`.

    The user's current-turn text feeds `intercept.execute_command(user_intent=...)`
    — which the guard uses to judge whether proposed writes actually
    serve the admin's request. Rebuilding per dispatch keeps it fresh.
    """
    from claude_agent_sdk import create_sdk_mcp_server, tool

    def _wrap(res: dict[str, Any]) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": json.dumps(res)}]}

    # --- read / visibility ---

    @tool("list_issues", "List GitHub issues.", {"state": str, "limit": int})
    async def list_issues_tool(args: dict[str, Any]) -> dict[str, Any]:
        return _wrap(
            await do_list_issues(
                state=str(args.get("state", "open")),
                limit=int(args.get("limit", 30)),
                user_intent=user_intent,
            )
        )

    @tool("view_issue", "View a single issue by number.", {"number": int})
    async def view_issue_tool(args: dict[str, Any]) -> dict[str, Any]:
        return _wrap(
            await do_view_issue(int(args["number"]), user_intent=user_intent)
        )

    @tool("list_prs", "List pull requests.", {"state": str, "limit": int})
    async def list_prs_tool(args: dict[str, Any]) -> dict[str, Any]:
        return _wrap(
            await do_list_prs(
                state=str(args.get("state", "open")),
                limit=int(args.get("limit", 30)),
                user_intent=user_intent,
            )
        )

    @tool("view_pr", "View a single PR by number.", {"number": int})
    async def view_pr_tool(args: dict[str, Any]) -> dict[str, Any]:
        return _wrap(
            await do_view_pr(int(args["number"]), user_intent=user_intent)
        )

    @tool(
        "list_project_items",
        "List items on a Projects-v2 board.",
        {"project_number": int, "owner": str, "limit": int},
    )
    async def list_project_items_tool(args: dict[str, Any]) -> dict[str, Any]:
        return _wrap(
            await do_list_project_items(
                project_number=int(args["project_number"]),
                owner=str(args["owner"]),
                limit=int(args.get("limit", 100)),
                user_intent=user_intent,
            )
        )

    # --- PM writes ---

    @tool(
        "comment_on_issue",
        "Post a comment on an issue.",
        {"number": int, "body": str},
    )
    async def comment_on_issue_tool(args: dict[str, Any]) -> dict[str, Any]:
        return _wrap(
            await do_comment_on_issue(
                number=int(args["number"]),
                body=str(args["body"]),
                user_intent=user_intent,
            )
        )

    @tool(
        "edit_issue",
        "Edit an issue: add/remove labels, add assignees, change title or milestone.",
        {
            "number": int,
            "add_labels": list,
            "remove_labels": list,
            "add_assignees": list,
            "title": str,
            "milestone": str,
        },
    )
    async def edit_issue_tool(args: dict[str, Any]) -> dict[str, Any]:
        return _wrap(
            await do_edit_issue(
                number=int(args["number"]),
                add_labels=args.get("add_labels"),
                remove_labels=args.get("remove_labels"),
                add_assignees=args.get("add_assignees"),
                title=args.get("title"),
                milestone=args.get("milestone"),
                user_intent=user_intent,
            )
        )

    @tool(
        "close_issue",
        "Close an issue. Optional reason (completed / not planned) and comment.",
        {"number": int, "reason": str, "comment": str},
    )
    async def close_issue_tool(args: dict[str, Any]) -> dict[str, Any]:
        return _wrap(
            await do_close_issue(
                number=int(args["number"]),
                reason=args.get("reason"),
                comment=args.get("comment"),
                user_intent=user_intent,
            )
        )

    @tool(
        "reopen_issue",
        "Reopen a closed issue, optionally with a comment.",
        {"number": int, "comment": str},
    )
    async def reopen_issue_tool(args: dict[str, Any]) -> dict[str, Any]:
        return _wrap(
            await do_reopen_issue(
                number=int(args["number"]),
                comment=args.get("comment"),
                user_intent=user_intent,
            )
        )

    @tool(
        "create_issue",
        "Create a new issue.",
        {"title": str, "body": str, "labels": list, "assignees": list},
    )
    async def create_issue_tool(args: dict[str, Any]) -> dict[str, Any]:
        return _wrap(
            await do_create_issue(
                title=str(args["title"]),
                body=str(args["body"]),
                labels=args.get("labels"),
                assignees=args.get("assignees"),
                user_intent=user_intent,
            )
        )

    @tool(
        "edit_project_field",
        "Set a custom field on a Projects-v2 item. Use node IDs from list_project_items.",
        {
            "project_number": int,
            "owner": str,
            "item_id": str,
            "field_id": str,
            "value": str,
        },
    )
    async def edit_project_field_tool(args: dict[str, Any]) -> dict[str, Any]:
        return _wrap(
            await do_edit_project_field(
                project_number=int(args["project_number"]),
                owner=str(args["owner"]),
                item_id=str(args["item_id"]),
                field_id=str(args["field_id"]),
                value=str(args["value"]),
                user_intent=user_intent,
            )
        )

    # --- pipeline: code-writing ---

    @tool(
        "run_moderate_issues",
        "Run 99-tools/moderate_issues.py. Formats messy issues into well-structured tasks.",
        {"issue": int, "max_tokens": int},
    )
    async def run_moderate_tool(args: dict[str, Any]) -> dict[str, Any]:
        return _wrap(
            await do_run_moderate_issues(
                issue=args.get("issue"),
                max_tokens=args.get("max_tokens"),
                user_intent=user_intent,
            )
        )

    @tool(
        "run_solve_issues",
        "Run 99-tools/solve_issues.py for a single issue. Writes code, opens a PR.",
        {"issue": int, "max_tokens": int},
    )
    async def run_solve_tool(args: dict[str, Any]) -> dict[str, Any]:
        return _wrap(
            await do_run_solve_issues(
                issue=int(args["issue"]),
                max_tokens=args.get("max_tokens"),
                user_intent=user_intent,
            )
        )

    @tool(
        "run_fix_prs",
        "Run 99-tools/fix_prs.py for a single PR. Reads review comments, pushes fixes.",
        {"pr": int, "max_tokens": int},
    )
    async def run_fix_tool(args: dict[str, Any]) -> dict[str, Any]:
        return _wrap(
            await do_run_fix_prs(
                pr=int(args["pr"]),
                max_tokens=args.get("max_tokens"),
                user_intent=user_intent,
            )
        )

    # --- pipeline: visibility (stubs until P4.12–16) ---

    @tool("run_sync_issues", "Pull issues + project fields from GitHub (pipeline).", {})
    async def run_sync_tool(args: dict[str, Any]) -> dict[str, Any]:
        return _wrap(await do_run_sync_issues(user_intent=user_intent))

    @tool("run_heuristics", "Infer durations / dependencies / delays (pipeline).", {})
    async def run_heuristics_tool(args: dict[str, Any]) -> dict[str, Any]:
        return _wrap(await do_run_heuristics(user_intent=user_intent))

    @tool(
        "run_render_chart",
        "Render a chart template (gantt / kanban / burndown / comparison).",
        {"template": str},
    )
    async def run_render_tool(args: dict[str, Any]) -> dict[str, Any]:
        return _wrap(
            await do_run_render_chart(
                template=str(args["template"]),
                user_intent=user_intent,
            )
        )

    @tool(
        "run_scenario",
        "A/B timeline comparison (e.g. delay='Issue #12: +14d').",
        {"delay": str},
    )
    async def run_scenario_tool(args: dict[str, Any]) -> dict[str, Any]:
        return _wrap(
            await do_run_scenario(
                delay=str(args["delay"]),
                user_intent=user_intent,
            )
        )

    # --- control (local, no guard) ---

    @tool("loop_pause", "Pause the autonomous loop (soft pause; schedule keeps ticking).", {})
    async def loop_pause_tool(args: dict[str, Any]) -> dict[str, Any]:
        return _wrap(await do_loop_pause())

    @tool("loop_resume", "Resume the autonomous loop.", {})
    async def loop_resume_tool(args: dict[str, Any]) -> dict[str, Any]:
        return _wrap(await do_loop_resume())

    @tool(
        "loop_scope",
        "Restrict the autonomous loop to specific issues and/or PRs.",
        {"only_issues": list, "only_prs": list},
    )
    async def loop_scope_tool(args: dict[str, Any]) -> dict[str, Any]:
        return _wrap(
            await do_loop_scope(
                only_issues=args.get("only_issues"),
                only_prs=args.get("only_prs"),
            )
        )

    @tool("loop_clear_scope", "Clear any loop scope restriction.", {})
    async def loop_clear_tool(args: dict[str, Any]) -> dict[str, Any]:
        return _wrap(await do_loop_clear_scope())

    @tool(
        "get_budgets",
        "Read the current token / edit / task budgets. Read-only.",
        {},
    )
    async def get_budgets_tool(args: dict[str, Any]) -> dict[str, Any]:
        return _wrap(await do_get_budgets())

    # --- escape hatch ---

    @tool(
        "run_shell",
        "Run an arbitrary argv through intercept.py (guard + budget enforced). "
        "Prefer named tools; use this only for commands no named tool covers.",
        {"argv": list, "rationale": str},
    )
    async def run_shell_tool(args: dict[str, Any]) -> dict[str, Any]:
        argv = args.get("argv") or []
        if not isinstance(argv, list) or not all(isinstance(x, str) for x in argv):
            return _wrap({"error": f"argv must be a list of strings, got {argv!r}"})
        return _wrap(
            await do_run_shell(
                argv,
                user_intent=user_intent,
                rationale=str(args.get("rationale") or "run_shell escape hatch"),
            )
        )

    return create_sdk_mcp_server(
        name="imp_foreman",
        tools=[
            list_issues_tool,
            view_issue_tool,
            list_prs_tool,
            view_pr_tool,
            list_project_items_tool,
            comment_on_issue_tool,
            edit_issue_tool,
            close_issue_tool,
            reopen_issue_tool,
            create_issue_tool,
            edit_project_field_tool,
            run_moderate_tool,
            run_solve_tool,
            run_fix_tool,
            run_sync_tool,
            run_heuristics_tool,
            run_render_tool,
            run_scenario_tool,
            loop_pause_tool,
            loop_resume_tool,
            loop_scope_tool,
            loop_clear_tool,
            get_budgets_tool,
            run_shell_tool,
        ],
    )


# Belt-and-suspenders: if the SDK ever defaults a tool to allowed, this
# denies it. All shell paths go through our MCP tools.
_DISALLOWED_TOOLS: tuple[str, ...] = (
    "Bash",
    "Edit",
    "Write",
    "Read",
    "Glob",
    "Grep",
    "NotebookEdit",
    "WebFetch",
    "WebSearch",
    "Task",
    "TodoWrite",
)


# ---------- dispatch driver ----------


SayFn = Callable[[str], Awaitable[None]]
AskFn = Callable[[str], Awaitable[Optional[str]]]
ThinkingFn = Callable[[str], Any]
# `chart(artifact)` is called once per chart artifact produced during
# the turn. The UI layer picks the right Chainlit element from the
# artifact descriptor (`{type, name, path}`) — main.py wires this to
# `cl.Plotly` for `type=="plotly"`. None disables chart rendering.
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
) -> None:
    """Run one Foreman conversation turn for `user_text`.

    Each call is a fresh `ClaudeSDKClient` session — no memory across
    user messages in this phase. Multi-turn conversation memory lands
    with later phases; for now the LLM gets user_text + current state
    (via tool calls) and produces a single-turn response.

    The `thinking` seam brackets the SDK call with a cl.Step spinner
    in the UI layer (same pattern as dispatcher.py's synthesis turn).
    The `chart` seam receives any chart artifacts produced by tool
    calls (currently `run_render_chart`) so they render inline in
    chat — Chainlit doesn't render mermaid blocks, so we render them
    as `cl.Plotly` figures instead.
    """
    # Reset the per-turn artifact collector so this dispatch only sees
    # charts created by its own tool calls.
    _pending_artifacts.clear()
    import sys

    print(f"[foreman] dispatch called: user_text={user_text!r}", file=sys.stderr)

    from claude_agent_sdk import (  # type: ignore[import-not-found]
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKClient,
        ResultMessage,
        TextBlock,
        ToolUseBlock,
    )

    mcp_server = _build_mcp_server(user_intent=user_text)

    # `allowed_tools` uses the SDK's mcp__<server>__<tool> convention.
    allowed_tool_names = [
        f"mcp__imp_foreman__{name}"
        for name in (
            "list_issues",
            "view_issue",
            "list_prs",
            "view_pr",
            "list_project_items",
            "comment_on_issue",
            "edit_issue",
            "close_issue",
            "reopen_issue",
            "create_issue",
            "edit_project_field",
            "run_moderate_issues",
            "run_solve_issues",
            "run_fix_prs",
            "run_sync_issues",
            "run_heuristics",
            "run_render_chart",
            "run_scenario",
            "loop_pause",
            "loop_resume",
            "loop_scope",
            "loop_clear_scope",
            "get_budgets",
            "run_shell",
        )
    ]

    options = ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        mcp_servers={"imp_foreman": mcp_server},
        allowed_tools=allowed_tool_names,
        disallowed_tools=list(_DISALLOWED_TOOLS),
        max_turns=20,
    )

    cm_factory = thinking if thinking is not None else (lambda _label: _NullAsyncContext())

    assistant_chunks: list[str] = []
    tool_calls_seen: list[str] = []

    try:
        async with cm_factory("Foreman is thinking…"):
            async with ClaudeSDKClient(options=options) as client:
                await client.query(user_text)
                async for message in client.receive_response():
                    if isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, TextBlock):
                                assistant_chunks.append(block.text)
                            elif isinstance(block, ToolUseBlock):
                                tool_calls_seen.append(block.name)
                                # Mirror tool calls into the chat so the
                                # admin sees what Foreman is doing before
                                # the final reply.
                                args_preview = (
                                    json.dumps(block.input, indent=2)
                                    if block.input
                                    else "{}"
                                )
                                await say(
                                    f"_Using tool:_ `{block.name}`\n"
                                    f"```json\n{args_preview}\n```"
                                )
                    elif isinstance(message, ResultMessage):
                        usage = getattr(message, "usage", None) or {}
                        in_tok = int(usage.get("input_tokens", 0) or 0)
                        out_tok = int(usage.get("output_tokens", 0) or 0)
                        if in_tok > 0 or out_tok > 0:
                            budgets.add_tokens(in_tok, out_tok)
    except Exception as exc:  # noqa: BLE001 — surface backend errors
        print(
            f"[foreman] backend error: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        await say(f"Foreman backend error: {exc}")
        return

    reply = "".join(assistant_chunks).strip()
    if reply:
        await say(reply)
    else:
        # A turn that produced only tool calls with no prose — tell the
        # admin something landed so they aren't staring at silence.
        if tool_calls_seen:
            await say(
                f"_(Foreman used {len(tool_calls_seen)} tool call(s) "
                f"but produced no prose reply. Ask a follow-up for a summary.)_"
            )

    # Emit any chart artifacts collected during the turn. The chart
    # callable is responsible for translating each descriptor into a
    # Chainlit element (cl.Plotly for `type=="plotly"`).
    if chart is not None and _pending_artifacts:
        for artifact in list(_pending_artifacts):
            try:
                await chart(artifact)
            except Exception as exc:  # noqa: BLE001 — don't let chart UI bugs swallow the turn
                print(
                    f"[foreman] chart render failed for {artifact!r}: "
                    f"{type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )

    print(
        f"[foreman] dispatch complete: tool_calls={tool_calls_seen} "
        f"reply_chars={len(reply)} chart_artifacts={len(_pending_artifacts)}",
        file=sys.stderr,
    )
