"""Imp — Chainlit spike (throwaway).

A stub demo of how Imp would feel if we use Chainlit instead of building the
FastAPI + WebSocket + JS frontend ourselves. Every interaction is faked: no
real Claude API calls, no real GitHub writes, no real venv work — the goal
is to evaluate the UX and decide whether to commit to this stack.

Try these messages after logging in:

  - "show me the gantt chart"
  - "moderate issue 42"  (or any number)
  - "what's the budget?"
  - "pause the loop"
  - "scope to 42, 43"

To re-run the Setup Agent, delete .imp/config.json and refresh.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import chainlit as cl

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / ".imp" / "config.json"
SPIKE_PASSWORD = os.environ.get("IMP_SPIKE_PASSWORD", "imp")


# ---------- config helpers ----------


def load_config() -> dict:
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {}


def save_config(cfg: dict) -> None:
    CONFIG_FILE.parent.mkdir(exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


def is_setup_complete() -> bool:
    return load_config().get("setup_complete", False)


# ---------- auth ----------


@cl.password_auth_callback
def auth(username: str, password: str) -> cl.User | None:
    """Single-admin auth. Username is ignored; only the password matters."""
    if password == SPIKE_PASSWORD:
        return cl.User(identifier="admin", metadata={"role": "admin"})
    return None


# ---------- chat lifecycle ----------


@cl.on_chat_start
async def on_start() -> None:
    if not is_setup_complete():
        await run_setup_agent()
    else:
        await greet_foreman()


# ---------- setup agent (stub) ----------


async def run_setup_agent() -> None:
    await cl.Message(
        author="Setup Agent",
        content=(
            "Hi — I'm the **Setup Agent**. I'll walk you through configuring Imp "
            "for your GitHub project.\n\n"
            "_(This is a stub spike. In the real version I'd call gh and Claude "
            "tools to actually do these things — here every step is faked so "
            "you can see the conversational flow.)_\n\n"
            "Let's start by picking a repo. Which one would you like Imp to manage?"
        ),
    ).send()

    repo_msg = await cl.AskUserMessage(
        content="Type a repo as `owner/name` (anything works for the spike):",
        timeout=300,
    ).send()
    if not repo_msg:
        return
    repo = repo_msg.get("output", "you/your-repo")

    async with cl.Step(name="gh repo view", type="tool") as step:
        step.input = f"gh repo view {repo} --json defaultBranchRef,visibility"
        step.output = '{"defaultBranchRef":{"name":"main"},"visibility":"PUBLIC"}'

    await cl.Message(
        author="Setup Agent",
        content=f"✅ Got it: `{repo}`. Default branch is `main`, repo is public.",
    ).send()

    res = await cl.AskActionMessage(
        author="Setup Agent",
        content=(
            "Next: do you want me to bootstrap a Projects v2 board called `Imp` "
            "with the 7 custom fields (`duration_days`, `start_date`, `end_date`, "
            "`confidence`, `source`, `assignee_verified`, `depends_on`)?\n\n"
            "Without it, Imp runs in **read-only mode** — charts work, but I "
            "can't annotate issues."
        ),
        actions=[
            cl.Action(
                name="bootstrap",
                payload={"action": "bootstrap"},
                label="✅ Yes, create the project board",
            ),
            cl.Action(
                name="skip",
                payload={"action": "skip"},
                label="⏭ Skip (read-only mode)",
            ),
        ],
        timeout=300,
    ).send()

    bootstrap = bool(res and res.get("payload", {}).get("action") == "bootstrap")

    if bootstrap:
        async with cl.Step(name="gh project create --title Imp", type="tool") as step:
            step.input = "Creating Projects v2 board"
            step.output = "Created project #2 https://github.com/users/you/projects/2"

        async with cl.Step(name="gh project field-create (×7)", type="tool") as step:
            step.input = (
                "duration_days (NUMBER), start_date (DATE), end_date (DATE), "
                "confidence (SINGLE_SELECT), source (SINGLE_SELECT), "
                "assignee_verified (SINGLE_SELECT), depends_on (TEXT)"
            )
            step.output = "All 7 custom fields created on project #2"

    save_config(
        {
            "setup_complete": True,
            "repo": repo,
            "project_number": 2 if bootstrap else None,
            "read_only_mode": not bootstrap,
        }
    )

    mode = "read-only mode" if not bootstrap else "full mode"
    await cl.Message(
        author="Setup Agent",
        content=(
            f"🎉 Setup complete ({mode}). Handing off to **Foreman** now — "
            "your next message goes to the worker agent."
        ),
    ).send()
    await greet_foreman()


# ---------- foreman (stub) ----------


async def greet_foreman() -> None:
    cfg = load_config()
    repo = cfg.get("repo", "your repo")
    mode_note = " *(read-only mode — no writes)*" if cfg.get("read_only_mode") else ""
    await cl.Message(
        author="Foreman",
        content=(
            f"Welcome back. I'm **Foreman** — I manage `{repo}`{mode_note}.\n\n"
            "Try one of:\n"
            "- *show me the gantt chart*\n"
            "- *moderate issue 42*\n"
            "- *solve issue 7*\n"
            "- *what's the budget?*\n"
            "- *pause the loop*\n"
            "- *scope to 42, 43*\n\n"
            "_(Stub spike: every response is faked. The point is the UX, not the data.)_"
        ),
    ).send()


@cl.on_message
async def on_message(msg: cl.Message) -> None:
    if not is_setup_complete():
        await run_setup_agent()
        return

    text = msg.content.lower().strip()

    if any(k in text for k in ("gantt", "chart", "timeline")):
        await fake_chart_response()
    elif any(k in text for k in ("moderate", "solve", "fix")):
        await fake_proposed_action(msg.content)
    elif "budget" in text:
        await fake_budgets()
    elif "resume" in text:
        await cl.Message(
            author="Foreman",
            content="▶ Autonomous loop **resumed**. Next tick in 47 minutes.",
        ).send()
    elif "pause" in text:
        await cl.Message(
            author="Foreman",
            content=(
                "⏸ Autonomous loop **paused**. I'll only act when you ask me "
                "directly. Say *resume* to start it again."
            ),
        ).send()
    elif "scope" in text:
        await fake_scope(text)
    elif "reset" in text and "setup" in text:
        if CONFIG_FILE.exists():
            CONFIG_FILE.unlink()
        await cl.Message(
            author="Foreman",
            content="Setup state cleared. Refresh the page to re-run the Setup Agent.",
        ).send()
    else:
        await cl.Message(
            author="Foreman",
            content=(
                "I'm a stub spike, so my responses are limited. Try one of:\n"
                "- *gantt*\n"
                "- *moderate issue 42*\n"
                "- *budget*\n"
                "- *pause* / *resume*\n"
                "- *scope to 42, 43*\n"
                "- *reset setup* (re-runs the Setup Agent on next refresh)"
            ),
        ).send()


# ---------- stub responses ----------


async def fake_chart_response() -> None:
    async with cl.Step(name="python pipeline/sync_issues.py", type="tool") as step:
        step.input = "gh issue list --repo you/imp --json ..."
        step.output = "Synced 25 issues from GitHub"

    async with cl.Step(name="python pipeline/heuristics.py", type="tool") as step:
        step.input = "Inferring durations and dependencies from .imp/issues.json"
        step.output = "Enriched 25 issues; 7 missing duration_days (will use llm-low estimates)"

    async with cl.Step(
        name="python pipeline/render_chart.py --template gantt", type="tool"
    ) as step:
        step.output = "Rendered .imp/output/gantt.html (mermaid)"

    mermaid = """```mermaid
gantt
    title Imp v0.1 Build Phases (stub)
    dateFormat  YYYY-MM-DD
    section Phase 1
    Chat shell + auth     :p1, 2026-04-15, 7d
    section Phase 2
    Security spine        :p2, after p1, 5d
    section Phase 3
    Setup Agent           :p3, after p2, 4d
    section Phase 4
    Foreman + visibility  :p4, after p3, 12d
    section Phase 5
    Wire 99-tools         :p5, after p4, 5d
    section Phase 6
    Autonomous loop       :p6, after p5, 4d
    section Phase 7
    Polish                :p7, after p6, 3d
```"""
    await cl.Message(
        author="Foreman",
        content=f"Here's the current build timeline:\n\n{mermaid}",
    ).send()


async def fake_proposed_action(user_text: str) -> None:
    issue_match = re.search(r"\d+", user_text)
    issue = issue_match.group() if issue_match else "42"

    if "moderate" in user_text.lower():
        script = "moderate_issues.py"
        kind = "moderate"
        effect = (
            f"Read issue #{issue}, format it into a structured task, "
            f"add the `llm-ready` label."
        )
    elif "solve" in user_text.lower():
        script = "solve_issues.py"
        kind = "solve"
        effect = (
            f"Read issue #{issue}, write code on a new branch, push it, "
            f"open a PR. Counts as ~1 task and ~10 edits."
        )
    else:
        script = "fix_prs.py"
        kind = "fix"
        effect = f"Read review comments on PR #{issue}, push fixes."

    cmd = f"python 99-tools/{script} --{'pr' if kind == 'fix' else 'issue'} {issue}"

    async with cl.Step(
        name="Foreman → Guard Agent (checkpoint B)", type="tool"
    ) as step:
        step.input = (
            f"Worker proposes: {cmd}\n"
            f"Worker rationale: User asked me to {kind} {'PR' if kind == 'fix' else 'issue'} #{issue}\n"
            f"User's original intent: {user_text}"
        )
        step.output = (
            "Guard verdict: APPROVE\n"
            f'Reason: Proposed command targets {issue}, which matches the user intent.'
        )

    res = await cl.AskActionMessage(
        author="Foreman",
        content=(
            f"I'd like to run:\n\n"
            f"```\n{cmd}\n```\n\n"
            f"**Effect:** {effect}\n\n"
            f"_The Guard Agent already approved this (see step above), but I'm "
            f"asking you to confirm before I touch GitHub._"
        ),
        actions=[
            cl.Action(
                name="approve",
                payload={"action": "approve"},
                label="✅ Approve and run",
            ),
            cl.Action(
                name="reject",
                payload={"action": "reject"},
                label="❌ Reject",
            ),
        ],
        timeout=300,
    ).send()

    if res and res.get("payload", {}).get("action") == "approve":
        async with cl.Step(name=cmd, type="tool") as step:
            step.input = f"Reading issue #{issue}, planning..."
            step.output = (
                f"Branch p{kind}-{issue} created\n"
                f"Code written and committed\n"
                f"Pushed to origin\n"
                f"Opened PR #99: https://github.com/you/imp/pull/99"
            )
        await cl.Message(
            author="Foreman",
            content=(
                f"✅ Done. Opened a PR for issue #{issue}: "
                f"`https://github.com/you/imp/pull/99` (stub link)\n\n"
                f"Budget after this run: tokens 17,840 / 200,000 · "
                f"edits 14 / 50 · tasks 2 / 10"
            ),
        ).send()
    else:
        await cl.Message(
            author="Foreman",
            content="Skipped. Anything else?",
        ).send()


async def fake_budgets() -> None:
    await cl.Message(
        author="Foreman",
        content=(
            "**Budget status**\n\n"
            "| Counter | Used | Limit | Remaining |\n"
            "|---|---:|---:|---:|\n"
            "| Tokens | 14,213 | 200,000 | 185,787 |\n"
            "| Edits  | 3      | 50      | 47      |\n"
            "| Tasks  | 1      | 10      | 9       |\n\n"
            "Say *set token budget to N*, *reset edits*, etc., to change them."
        ),
    ).send()


async def fake_scope(text: str) -> None:
    issues = re.findall(r"\d+", text)
    if issues:
        ids = ", ".join(f"#{i}" for i in issues)
        await cl.Message(
            author="Foreman",
            content=(
                f"🎯 Loop **scoped to {ids}**. I'll only process those issues "
                f"on autonomous ticks until you say *clear scope*."
            ),
        ).send()
    else:
        await cl.Message(
            author="Foreman",
            content="Tell me which issues to scope to, e.g. *scope to 42, 43*.",
        ).send()
