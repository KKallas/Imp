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
import time
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from . import budgets, intercept

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / ".imp" / "config.json"

# Per-turn artifact collector for chat-renderable side outputs (charts,
# scenario grids, etc.). `dispatch()` clears this at the start of every
# turn and drains it at the end via the `chart` UI callable. Tool bodies
# append descriptors of type {"type": "...", ...} that `main.py`
# knows how to render. Currently only "scenario_session" is wired;
# "plotly" arrives with KKallas/Imp#14 follow-up work.
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


async def do_run_estimate_dates(
    *, user_intent: str, push: bool = False
) -> dict[str, Any]:
    """pipeline/estimate_dates.py — fills in missing `start_date` /
    `end_date` by running `synthesize_dates` over the enriched payload.

    Without `push`, the estimates stay local (updates `.imp/enriched.json`
    only). With `push=True`, each newly-estimated issue also gets its
    body updated with an `<!-- imp:dates -->` block via `gh issue edit`,
    so the estimate survives the next sync and shows up on github.com.

    This is Layer 1 of the gantt flow: estimate missing data first,
    then call `run_render_chart('gantt')` to render from the now-
    populated payload. For repos with a real GH Project attached, the
    project-board `start_date` / `end_date` fields always win — this
    pass only touches issues where they're absent.
    """
    argv = [sys.executable, "pipeline/estimate_dates.py"]
    if push:
        argv.append("--push")
    rationale = "estimate missing dates" + (" (push to GH)" if push else "")
    return await do_run_shell(argv, user_intent=user_intent, rationale=rationale)


async def do_run_render_chart(
    template: str, *, user_intent: str
) -> dict[str, Any]:
    """pipeline/render_chart.py — renders gantt/kanban/burndown/comparison.

    On success, pushes a `chart_file` artifact into `_pending_artifacts`
    so the chat layer can render the output inline. For `burndown` we
    additionally build a Plotly figure from the same enriched data —
    The chat layer screenshots the HTML to an inline PNG image (with a
    link to the interactive page), so the user sees the chart directly
    in the conversation.
    """
    argv = [sys.executable, "pipeline/render_chart.py", "--template", template]
    result = await do_run_shell(
        argv, user_intent=user_intent, rationale=f"render {template} chart"
    )

    if result.get("exit_code") == 0:
        html_path = _extract_render_chart_path(result.get("output") or "")
        plotly_figure = _build_plotly_for_chart_file(template)
        artifact: dict[str, Any] = {
            "type": "chart_file",
            "template": template,
            "path": str(html_path) if html_path else None,
            "plotly_figure": plotly_figure,
        }
        _pending_artifacts.append(artifact)

    return result


def _extract_render_chart_path(output: str) -> Path | None:
    """Find the rendered HTML path in `pipeline/render_chart.py`'s
    output. It prints the path on the last line of stdout, and a
    summary on stderr — but `intercept.execute_command` merges the two
    streams, so a plain "last line" parse is fragile (stderr can
    arrive after stdout). Scan every line and pick the last one that
    looks like a real `.html` path on disk.
    """
    match: Path | None = None
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped or not stripped.endswith(".html"):
            continue
        candidate = Path(stripped)
        if candidate.exists():
            match = candidate
    return match


def _build_plotly_for_chart_file(template: str) -> dict[str, Any] | None:
    """Build a Plotly figure dict for templates that have a native
    Plotly equivalent. Burndown is the only one today — gantt/kanban/
    comparison stay as HTML-only downloads until a Plotly port lands.

    Returns None on any failure — missing enriched.json, import error,
    unsupported template. Callers treat None as "no inline chart,
    only the HTML file will be attached."
    """
    if template != "burndown":
        return None
    enriched_path = ROOT / ".imp" / "enriched.json"
    if not enriched_path.exists():
        return None
    try:
        from pipeline import render_chart

        enriched = json.loads(enriched_path.read_text())
        context = render_chart.build_context_for_burndown(enriched)
        return render_chart.build_burndown_plotly_figure(context)
    except Exception as exc:  # noqa: BLE001 — UI helper, never raise
        print(
            f"[foreman] _build_plotly_for_chart_file({template!r}) failed: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return None


# (`do_run_scenario` / `run_scenario_tool` removed — KKallas/Imp#16
# ships the real scenario system via `start_scenario_session` + friends.
# The old stub shelled out to a non-existent `pipeline/scenario.py` and
# was a fallback magnet for Foreman when `start_scenario_session`
# rejected something.)


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


# ---------- scenario sessions (KKallas/Imp#16) ----------
#
# Start / commit / switch / close / open / list — the six-verb surface
# for the scenario-comparison flow. Tool bodies call into
# `pipeline/scenarios.py` (generator + runner + session I/O) and push
# a `{"type": "scenario_session", ...}` artifact into
# `_pending_artifacts` so `main.py` can render the grid + action buttons.


def _load_baseline_for_scenarios() -> dict[str, Any]:
    """Load `.imp/enriched.json` for scenario runs. If the user hasn't
    run sync + heuristics yet, return a minimal stub so the LLM sees a
    structured error via the scenario outputs rather than an exception."""
    enriched_path = ROOT / ".imp" / "enriched.json"
    if not enriched_path.exists():
        return {
            "issues": [],
            "issue_count": 0,
            "_warning": "no enriched.json — run sync + heuristics first",
        }
    try:
        return json.loads(enriched_path.read_text())
    except json.JSONDecodeError as exc:
        return {
            "issues": [],
            "issue_count": 0,
            "_warning": f"enriched.json unparseable: {exc}",
        }


async def do_start_scenario_session(descriptions: list[str]) -> dict[str, Any]:
    """Generate + save + run a scenario session. Pushes a grid artifact
    into _pending_artifacts for the chat layer to render."""
    from pipeline import scenarios

    if not isinstance(descriptions, list) or not all(isinstance(d, str) for d in descriptions):
        return {"error": "descriptions must be a list of strings"}
    descriptions = [d.strip() for d in descriptions if d.strip()]

    try:
        baseline = _load_baseline_for_scenarios()
        session_id, outs = await scenarios.start_session(descriptions, baseline)
    except scenarios.ScenarioValidationError as exc:
        return {"error": f"generated scenarios failed validation: {exc}"}
    except ValueError as exc:
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"scenario generation failed: {type(exc).__name__}: {exc}"}

    artifact = {
        "type": "scenario_session",
        "session_id": session_id,
        "descriptions": descriptions,
        "scenarios": [o.to_dict() for o in outs],
        "committed_choice": None,
        "baseline_empty": baseline.get("_warning") is not None,
    }
    _pending_artifacts.append(artifact)

    return {
        "session_id": session_id,
        "scenario_count": len(outs),
        "scenario_names": [o.name for o in outs],
        "message": (
            f"Scenario session `{session_id}` started with {len(outs)} scenarios. "
            f"The chat is now frozen until you commit to one or close the session. "
            f"Grid will render in the next message."
        ),
    }


async def do_commit_scenario(session_id: str, choice_index: int) -> dict[str, Any]:
    """Stage-1 commit: record the admin's scenario choice without
    mutating baseline data. Clears the chat-freeze."""
    from pipeline import scenarios

    try:
        baseline = _load_baseline_for_scenarios()
        committed = scenarios.commit_session(session_id, int(choice_index), baseline)
    except FileNotFoundError:
        return {"error": f"session {session_id!r} not found"}
    except ValueError as exc:
        return {"error": str(exc)}
    except scenarios.ScenarioValidationError as exc:
        return {"error": f"session source failed re-validation: {exc}"}

    return {"committed": committed, "message": f"Committed scenario '{committed['choice_name']}'."}


async def do_switch_scenario(session_id: str, choice_index: int) -> dict[str, Any]:
    """Switch the active commit to a different scenario. Same mechanics
    as commit; distinct tool name so the admin sees the distinction
    in the chat log."""
    return await do_commit_scenario(session_id, choice_index)


async def do_close_scenario(session_id: str) -> dict[str, Any]:
    """Close without committing. Session stays on disk (re-openable)."""
    from pipeline import scenarios

    scenarios.close_session(session_id)
    return {"closed": session_id, "message": f"Session `{session_id}` closed without commit."}


async def do_open_scenario_session(session_id: str) -> dict[str, Any]:
    """Re-run a saved session against current baseline data, push grid
    artifact + previously-committed choice if any."""
    from pipeline import scenarios

    descriptions = scenarios.load_session_descriptions(session_id)
    if not descriptions:
        return {"error": f"session {session_id!r} not found or has no descriptions"}

    baseline = _load_baseline_for_scenarios()
    try:
        outs = scenarios.run_session(session_id, baseline)
    except scenarios.ScenarioValidationError as exc:
        return {"error": f"session source failed validation on re-open: {exc}"}
    except FileNotFoundError:
        return {"error": f"session {session_id!r} source missing on disk"}

    committed_path = scenarios.session_dir(session_id) / "committed.json"
    committed: dict[str, Any] | None = None
    if committed_path.exists():
        try:
            committed = json.loads(committed_path.read_text())
        except json.JSONDecodeError:
            committed = None

    artifact = {
        "type": "scenario_session",
        "session_id": session_id,
        "descriptions": descriptions,
        "scenarios": [o.to_dict() for o in outs],
        "committed_choice": committed.get("choice_index") if committed else None,
        "baseline_empty": baseline.get("_warning") is not None,
    }
    _pending_artifacts.append(artifact)

    return {
        "session_id": session_id,
        "scenario_count": len(outs),
        "scenario_names": [o.name for o in outs],
        "committed_choice": committed.get("choice_index") if committed else None,
        "message": f"Reopened session `{session_id}`.",
    }


async def do_list_scenario_sessions(limit: int = 20) -> dict[str, Any]:
    """Return recent saved scenario sessions with their committed state."""
    from pipeline import scenarios

    rows = scenarios.list_sessions(limit=int(limit))
    return {"count": len(rows), "sessions": rows}


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
- `run_sync_issues` / `run_heuristics` / `run_render_chart(template)` — \
pipeline visibility scripts.
- `run_estimate_dates(push=false)` — fills in missing `start_date` / \
`end_date` by running `synthesize_dates`. **Call this before any gantt \
render when the repo has no linked project board** (or when the gantt \
produces 0 entries / a large "missing dates" list). With `push=true`, \
the estimates are written back to each issue's body on GitHub inside \
an `<!-- imp:dates -->` block so they survive the next sync. Default \
to `push=false` unless the admin explicitly asks to persist the \
estimates to github.com.

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

### Scenario comparison (KKallas/Imp#16 — FOR ANY "what if" / "compare" REQUEST)
- `start_scenario_session(descriptions: list[str])` — start a side-by-side \
comparison of 2-5 variants. Takes plain-English descriptions like \
`["as-is", "start 2 weeks from now", "4 devs not 2"]`. Generates a hidden \
Python file, runs it, renders a grid of charts + metrics, and **freezes the \
chat** until the admin commits to one or closes.
- `commit_scenario(session_id, choice_index)` — record the admin's choice. \
Usually driven by the admin clicking an action button; you normally won't \
call it directly.
- `switch_scenario(session_id, choice_index)` — change a prior commit.
- `close_scenario(session_id)` — close without committing.
- `open_scenario_session(session_id)` — re-run a saved session.
- `list_scenario_sessions(limit)` — list recent saved sessions.

**CRITICAL**: for any "compare / what-if / scenarios / A-vs-B" request, \
use `start_scenario_session`. Do NOT fall back to shelling out or \
building the comparison by hand — the scenario system gives you \
interactive Plotly + commit/switch buttons for free. If \
`start_scenario_session` returns an error (e.g. validation failure on \
the generated code), surface the error to the admin and stop; do not \
retry with shell commands.

### Control (local, no guard)
- `loop_pause` / `loop_resume` / `loop_scope(only_issues, only_prs)` / \
`loop_clear_scope`
- `get_budgets` — read-only. Admin changes limits via the gear-icon \
panel in the Chainlit UI; you cannot set or reset them.

### Escape hatch
- `run_shell(argv)` — any argv the classifier recognises. Prefer named \
tools when possible; fall back to this only when no named tool fits. \
**Never** use `run_shell` to substitute for a tool that exists (e.g. \
don't `run_shell cat .imp/enriched.json` when `list_issues` / scenarios \
give you structured data).

## How you respond

Plain markdown. You CAN use mermaid fenced code blocks in your replies \
— an automated watchdog screenshots them to inline PNG images with a \
link to the interactive viewer. For canonical project charts (gantt, \
burndown, kanban, comparison) prefer `run_render_chart` which also \
produces inline screenshots. For one-off or custom data, either use a \
mermaid code block or write a `python -c` script that builds a Plotly \
figure dict. Keep replies concise; the admin reads quickly.
"""


# ---------- MCP server factory ----------


def _build_mcp_server(
    user_intent: str, tracker: Optional[_ToolTracker] = None
) -> Any:
    """Build a fresh MCP server whose tool closures capture `user_intent`.

    When *tracker* is provided, each tool handler is wrapped so the
    tracker emits ``tool_started`` / ``tool_finished`` events to the
    ``TurnUI``.

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
        "run_estimate_dates",
        "Estimate missing start_date / end_date on every issue by "
        "running synthesize_dates over the enriched payload. Pass "
        "push=true to also persist the estimates to each issue's "
        "body on GitHub (so the dates survive the next sync and show "
        "on github.com). Layer 1 of the gantt flow — call this before "
        "run_render_chart('gantt') whenever the baseline data lacks "
        "project-board dates.",
        {"push": bool},
    )
    async def run_estimate_dates_tool(args: dict[str, Any]) -> dict[str, Any]:
        return _wrap(
            await do_run_estimate_dates(
                user_intent=user_intent,
                push=bool(args.get("push", False)),
            )
        )

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

    # (`run_scenario` tool removed — KKallas/Imp#16 ships
    # `start_scenario_session` as the real scenario surface.)

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

    # --- scenario sessions (KKallas/Imp#16) ---

    @tool(
        "start_scenario_session",
        "Start a scenario-comparison session. Takes 2-5 text descriptions "
        "(one per scenario, e.g. 'as-is', 'start 2 weeks from now', '4 devs "
        "not 2'). Generates a hidden Python file that produces a grid of "
        "charts + metrics side-by-side. The chat FREEZES until the admin "
        "commits to one scenario or closes the session.",
        {"descriptions": list},
    )
    async def start_scenario_tool(args: dict[str, Any]) -> dict[str, Any]:
        descriptions = args.get("descriptions") or []
        return _wrap(await do_start_scenario_session(descriptions))

    @tool(
        "commit_scenario",
        "Commit a choice in a scenario session. Baseline data on disk is "
        "NOT modified — commit is internal state the render pipeline uses "
        "as the active lens. Separate 'apply to project board' flow handles "
        "the real GitHub writes.",
        {"session_id": str, "choice_index": int},
    )
    async def commit_scenario_tool(args: dict[str, Any]) -> dict[str, Any]:
        return _wrap(
            await do_commit_scenario(
                str(args["session_id"]), int(args["choice_index"])
            )
        )

    @tool(
        "switch_scenario",
        "Change the committed choice on an existing session.",
        {"session_id": str, "choice_index": int},
    )
    async def switch_scenario_tool(args: dict[str, Any]) -> dict[str, Any]:
        return _wrap(
            await do_switch_scenario(
                str(args["session_id"]), int(args["choice_index"])
            )
        )

    @tool(
        "close_scenario",
        "Close a scenario session without committing. Session stays on "
        "disk (re-openable).",
        {"session_id": str},
    )
    async def close_scenario_tool(args: dict[str, Any]) -> dict[str, Any]:
        return _wrap(await do_close_scenario(str(args["session_id"])))

    @tool(
        "open_scenario_session",
        "Reopen a saved scenario session by id. Re-runs the saved .py "
        "against current baseline data and shows the grid again.",
        {"session_id": str},
    )
    async def open_scenario_tool(args: dict[str, Any]) -> dict[str, Any]:
        return _wrap(await do_open_scenario_session(str(args["session_id"])))

    @tool(
        "list_scenario_sessions",
        "List recent saved scenario sessions (newest first) with their "
        "descriptions and commit state.",
        {"limit": int},
    )
    async def list_scenarios_tool(args: dict[str, Any]) -> dict[str, Any]:
        return _wrap(await do_list_scenario_sessions(int(args.get("limit", 20))))

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

    all_tools = [
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
        loop_pause_tool,
        loop_resume_tool,
        loop_scope_tool,
        loop_clear_tool,
        get_budgets_tool,
        start_scenario_tool,
        commit_scenario_tool,
        switch_scenario_tool,
        close_scenario_tool,
        open_scenario_tool,
        list_scenarios_tool,
        run_shell_tool,
    ]

    # Wrap every handler so the tracker can emit per-tool events.
    if tracker is not None:
        for t in all_tools:
            _orig = t.handler
            _name = t.name

            async def _instrumented(
                args: dict[str, Any],
                *,
                _h: Any = _orig,
                _n: str = _name,
            ) -> dict[str, Any]:
                await tracker._on_start(_n)
                t0 = time.monotonic()
                try:
                    result = await _h(args)
                    is_err = result.get("is_error", False)
                    out_text = ""
                    for item in result.get("content", []):
                        if item.get("type") == "text":
                            out_text += item.get("text", "")
                    await tracker._on_done(
                        _n, not is_err, time.monotonic() - t0, out_text
                    )
                    return result
                except Exception as exc:
                    await tracker._on_done(
                        _n, False, time.monotonic() - t0, str(exc)
                    )
                    raise

            t.handler = _instrumented

    return create_sdk_mcp_server(name="imp_foreman", tools=all_tools)


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


# ---------- structured turn UI (KKallas/Imp#55) ----------

# MCP tool names are prefixed by the SDK; strip for display.
_MCP_PREFIX = "mcp__imp_foreman__"


def _clean_tool_name(name: str) -> str:
    """Strip the MCP server prefix from a tool name."""
    return name[len(_MCP_PREFIX) :] if name.startswith(_MCP_PREFIX) else name


def _format_tool_sig(name: str, args: dict[str, Any]) -> str:
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

    name: str  # clean name (no MCP prefix)
    args: dict[str, Any]
    status: str = "pending"  # pending | running | ok | error
    duration_s: float = 0.0
    output: str = ""


class TurnUI:
    """Callback interface for structured tool-call rendering.

    Base class provides no-op methods so ``dispatch`` can call them
    unconditionally.  ``main.py`` subclasses this to implement
    Chainlit rendering (plan checklist, tool steps, streaming text,
    foldable thinking).
    """

    async def show_plan(self, items: list[PlanItem]) -> None:
        """Display the initial plan checklist (⏳ for every item)."""

    async def append_plan(self, items: list[PlanItem]) -> None:
        """Add follow-up-wave items to an existing plan."""

    async def tool_started(self, index: int, item: PlanItem) -> None:
        """A tool started executing (index into the plan list)."""

    async def tool_finished(self, index: int, item: PlanItem) -> None:
        """A tool finished (ok or error)."""

    async def stream_token(self, token: str) -> None:
        """Append one token of assistant prose."""

    async def stream_end(self, full_text: str) -> None:
        """Finalise streamed text (post-processing, mermaid, etc.)."""

    async def thinking_update(self, text: str) -> None:
        """Append to the foldable thinking step."""


class _ToolTracker:
    """Wraps MCP tool handlers to emit per-tool start/finish events.

    Created per-dispatch.  ``_build_mcp_server`` injects the tracker
    into every tool handler so status updates flow to ``TurnUI``
    without the tool bodies knowing about the UI.
    """

    def __init__(self, turn_ui: TurnUI) -> None:
        self.turn_ui = turn_ui
        self.plan_items: list[PlanItem] = []
        # Queue of plan-list indices per clean tool name so we can
        # match the (name-only) MCP callback to the right row when
        # the same tool is called multiple times in one batch.
        self._pending: dict[str, list[int]] = {}

    def register_batch(self, tool_blocks: list[Any]) -> list[PlanItem]:
        """Register a batch of ``ToolUseBlock``s, return new items."""
        new_items: list[PlanItem] = []
        for block in tool_blocks:
            clean = _clean_tool_name(block.name)
            item = PlanItem(name=clean, args=block.input or {})
            idx = len(self.plan_items)
            self.plan_items.append(item)
            self._pending.setdefault(clean, []).append(idx)
            new_items.append(item)
        return new_items

    async def _on_start(self, tool_name: str) -> None:
        indices = self._pending.get(tool_name, [])
        if not indices:
            return
        idx = indices[0]  # peek — pop on done
        self.plan_items[idx].status = "running"
        await self.turn_ui.tool_started(idx, self.plan_items[idx])

    async def _on_done(
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


# ---------- dispatch driver ----------


SayFn = Callable[[str], Awaitable[None]]
AskFn = Callable[[str], Awaitable[Optional[str]]]
ThinkingFn = Callable[[str], Any]
# `chart(artifact)` is called once per artifact descriptor accumulated
# during the turn. Currently used for `{type: "scenario_session", ...}`
# (KKallas/Imp#16) — main.py renders the side-by-side grid + commit
# action buttons. None disables artifact rendering (tests / headless).
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
    """Run one Foreman conversation turn for ``user_text``.

    Each call builds a fresh ``ClaudeSDKClient`` — we keep no persistent
    client across turns — but *history* (a list of ``chat_history.Turn``)
    is flattened into a preamble and prepended to the user message so
    the agent can reference earlier turns.

    Returns the plain-prose assistant reply so the caller can append
    it to the session history.  Returns an empty string when the LLM
    produced only tool calls / artifacts and no prose.

    When *turn_ui* is provided the structured plan→execute→reason flow
    is used (KKallas/Imp#55): tool calls are rendered as a checklist
    with per-tool status, text is streamed token-by-token, and thinking
    blocks feed a foldable step.  When ``None``, the legacy per-tool
    ``say()`` behaviour is preserved (tests / headless).
    """
    from server import chat_history

    _pending_artifacts.clear()

    print(f"[foreman] dispatch called: user_text={user_text!r}", file=sys.stderr)

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

    # Set up the tool tracker when the structured UI is active.
    ui = turn_ui or TurnUI()  # base TurnUI = no-ops
    tracker = _ToolTracker(ui) if turn_ui is not None else None
    mcp_server = _build_mcp_server(user_intent=user_text, tracker=tracker)

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
            "loop_pause",
            "loop_resume",
            "loop_scope",
            "loop_clear_scope",
            "get_budgets",
            "start_scenario_session",
            "commit_scenario",
            "switch_scenario",
            "close_scenario",
            "open_scenario_session",
            "list_scenario_sessions",
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
    has_plan = False

    try:
        async with cm_factory("Foreman is thinking…"):
            async with ClaudeSDKClient(options=options) as client:
                await client.query(prompt_text)
                async for message in client.receive_response():
                    if isinstance(message, AssistantMessage):
                        # Collect blocks by type first, then process in
                        # the right visual order: thinking → plan → text.
                        # This prevents preamble text ("Sure, let me…")
                        # from creating an answer message before the plan.
                        msg_thinking: list[Any] = []
                        msg_tools: list[Any] = []
                        msg_text: list[Any] = []
                        for block in message.content:
                            if isinstance(block, ThinkingBlock):
                                msg_thinking.append(block)
                            elif isinstance(block, ToolUseBlock):
                                tool_calls_seen.append(block.name)
                                if block.name.startswith(_MCP_PREFIX):
                                    msg_tools.append(block)
                            elif isinstance(block, TextBlock):
                                msg_text.append(block)

                        # 1. Thinking (buffered in UI until answer)
                        for b in msg_thinking:
                            await ui.thinking_update(b.thinking)

                        # 2. Plan checklist
                        if msg_tools and tracker is not None:
                            new_items = tracker.register_batch(msg_tools)
                            if not has_plan:
                                await ui.show_plan(tracker.plan_items)
                                has_plan = True
                            else:
                                await ui.append_plan(new_items)
                        elif msg_tools and turn_ui is None:
                            for block in msg_tools:
                                args_preview = (
                                    json.dumps(block.input, indent=2)
                                    if block.input
                                    else "{}"
                                )
                                await say(
                                    f"_Using tool:_ `{block.name}`\n"
                                    f"```json\n{args_preview}\n```"
                                )

                        # 3. Text — always accumulate for the final
                        #    reply, but only stream if this message has
                        #    no tool calls (preamble text in a tool-
                        #    bearing message is deferred so the plan
                        #    and tool steps appear first in the chat).
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

    except Exception as exc:  # noqa: BLE001 — surface backend errors
        print(
            f"[foreman] backend error: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        await say(f"Foreman backend error: {exc}")
        return ""

    reply = "".join(assistant_chunks).strip()

    if turn_ui is not None:
        # Structured UI handled text streaming — finalise it.
        if reply:
            await ui.stream_end(reply)
        elif tool_calls_seen:
            await say(
                f"_(Foreman used {len(tool_calls_seen)} tool call(s) "
                f"but produced no prose reply. Ask a follow-up for a summary.)_"
            )
    else:
        # Legacy: post the buffered reply as a single message.
        if reply:
            await say(reply)
        elif tool_calls_seen:
            await say(
                f"_(Foreman used {len(tool_calls_seen)} tool call(s) "
                f"but produced no prose reply. Ask a follow-up for a summary.)_"
            )

    # Drain pending artifacts (scenario grids, etc.).
    if chart is not None and _pending_artifacts:
        for artifact in list(_pending_artifacts):
            try:
                await chart(artifact)
            except Exception as exc:  # noqa: BLE001 — UI bugs shouldn't kill the turn
                print(
                    f"[foreman] chart render failed for {artifact.get('type')!r}: "
                    f"{type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )

    print(
        f"[foreman] dispatch complete: tool_calls={tool_calls_seen} "
        f"reply_chars={len(reply)} artifacts={len(_pending_artifacts)}",
        file=sys.stderr,
    )
    return reply
