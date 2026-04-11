"""Imp — Chainlit app.

The entire frontend of Imp. Chainlit owns the wire; everything the user sees
is produced by the handlers in this file calling Chainlit primitives.

At this stage (P1.2) the Setup Agent and Foreman handlers are still largely
stubbed — they demonstrate the UX and exercise the auth / dispatch / message
plumbing, but the real agents replace them in later phases (see KKallas/Imp#9
for setup_agent.py and KKallas/Imp#11 for foreman_agent.py).

Auth and the bootstrap flow are real as of P1.2:
  - Single-admin argon2id password hash stored in .imp/config.json
  - Bootstrap mode: when no hash is set yet, any password logs in, and the
    Setup Agent immediately asks the admin to pick a real password that gets
    hashed + persisted before the rest of setup runs.

Try these messages after logging in (stub responses until later phases):
  - "show me the gantt chart"
  - "moderate issue 42"  (or any number)
  - "what's the budget?"
  - "pause the loop"
  - "scope to 42, 43"
  - "reset setup"

To re-run the Setup Agent from scratch, delete .imp/config.json and refresh.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import chainlit as cl
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / ".imp" / "config.json"

_hasher = PasswordHasher()


# ---------- config helpers ----------


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_config(cfg: dict) -> None:
    CONFIG_FILE.parent.mkdir(exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


def is_setup_complete() -> bool:
    return load_config().get("setup_complete", False)


def has_admin_password() -> bool:
    return bool(load_config().get("admin_password_hash"))


# ---------- password helpers ----------


def hash_password(plain: str) -> str:
    """Hash a plaintext password with argon2id."""
    return _hasher.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """Return True if plain matches the stored argon2 hash."""
    try:
        _hasher.verify(hashed, plain)
        return True
    except VerifyMismatchError:
        return False


def set_admin_password(plain: str) -> None:
    """Hash the password and persist it to .imp/config.json."""
    cfg = load_config()
    cfg["admin_password_hash"] = hash_password(plain)
    save_config(cfg)


# ---------- auth ----------


@cl.password_auth_callback
def auth(username: str, password: str) -> cl.User | None:
    """Single-admin auth.

    If no admin_password_hash is set in config (fresh install / bootstrap
    mode), accept any password — the first chat session will immediately be
    routed to the Setup Agent which sets a real password before doing
    anything else.

    Once a hash is set, the password must verify against it.

    Username is ignored; this is a single-admin deployment.
    """
    cfg = load_config()
    hashed = cfg.get("admin_password_hash")
    if not hashed:
        # Bootstrap mode — no password set yet.
        return cl.User(
            identifier="admin",
            metadata={"role": "admin", "bootstrap": True},
        )
    if verify_password(password, hashed):
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
            "Hi — I'm the **Setup Agent**. I'll walk you through configuring "
            "Imp for your GitHub project.\n\n"
            "_(At this stage most steps are still stubs. Real `gh` auth, real "
            "Claude auth, real repo listing, and real project board bootstrap "
            "land in later phases — see KKallas/Imp#9 and KKallas/Imp#10. The "
            "password-setting step below is real now.)_"
        ),
    ).send()

    # Step 0: set a real password if we're in bootstrap mode.
    if not has_admin_password():
        await cl.Message(
            author="Setup Agent",
            content=(
                "First, let's set an admin password. Right now anyone who can "
                "reach this URL can log in with any password — let's fix that.\n\n"
                "Pick a password you'll remember. I'll hash it with argon2id "
                "and store the hash (not the plaintext) in `.imp/config.json`. "
                "From the next login onwards, only this password works."
            ),
        ).send()

        pw_msg = await cl.AskUserMessage(
            content="Type your admin password:",
            timeout=300,
        ).send()
        if not pw_msg:
            return
        plain = pw_msg.get("output", "").strip()
        if len(plain) < 4:
            await cl.Message(
                author="Setup Agent",
                content=(
                    "That's too short. Pick something at least 4 characters. "
                    "Say *reset setup* and try again."
                ),
            ).send()
            return

        async with cl.Step(name="argon2id.hash(password)", type="tool") as step:
            step.input = "<redacted>"
            set_admin_password(plain)
            step.output = (
                f"Hash stored in .imp/config.json → "
                f"admin_password_hash (argon2id, "
                f"{len(load_config()['admin_password_hash'])} chars)"
            )

        await cl.Message(
            author="Setup Agent",
            content=(
                "✅ Password set. Bootstrap mode is off — from the next login, "
                "that password is required."
            ),
        ).send()

    # Step 1: pick a repo (still stubbed until KKallas/Imp#9).
    await cl.Message(
        author="Setup Agent",
        content="Now let's pick the GitHub repo Imp will manage.",
    ).send()

    repo_msg = await cl.AskUserMessage(
        content="Type a repo as `owner/name` (anything works at this stage):",
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

    # Merge into existing config so we don't blow away the password hash
    # set earlier in this conversation.
    cfg = load_config()
    cfg.update(
        {
            "setup_complete": True,
            "repo": repo,
            "project_number": 2 if bootstrap else None,
            "read_only_mode": not bootstrap,
        }
    )
    save_config(cfg)

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
        # Clear everything EXCEPT the admin password hash. "reset setup"
        # re-runs the Setup Agent (repo, project board, loop config) but
        # keeps the admin authenticated. To wipe the password too, the
        # admin edits .imp/config.json on disk or deletes it outright.
        cfg = load_config()
        preserved = {}
        if cfg.get("admin_password_hash"):
            preserved["admin_password_hash"] = cfg["admin_password_hash"]
        save_config(preserved)
        await cl.Message(
            author="Foreman",
            content=(
                "Setup state cleared (password kept). Refresh the page to "
                "re-run the Setup Agent. To wipe the password too, delete "
                "`.imp/config.json` from disk."
            ),
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
