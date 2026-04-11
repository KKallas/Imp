"""Imp — Chainlit app.

The entire frontend of Imp. Chainlit owns the wire; everything the user sees
is produced by the handlers in this file calling Chainlit primitives.

Auth is real as of P1.2: single-admin argon2id hash stored in
.imp/config.json, seeded by imp.py via a getpass prompt on very first run.
There is no bootstrap mode in the browser — by the time Chainlit starts,
the hash is either there or the user was given a chance to set it in the
terminal.

Setup Agent is partially real as of P1.2:
  - Checks `gh auth status`; if not authenticated, tells the user to run
    `gh auth login --web` in a terminal and waits for them to say "ready"
  - Detects the target repo from `git remote get-url origin` — Imp is
    expected to live inside the repo it manages, so the local git context
    IS the config. If no origin is found, asks the user manually.
  - Verifies the repo exists via `gh repo view`
  - Stops there. Project-board bootstrap, loop config, etc. land in
    KKallas/Imp#10, KKallas/Imp#23, etc.

Foreman is still stubbed — the chat-command echo responses exercise the
dispatch UX but produce no real work. Replaced by the real worker agent
in KKallas/Imp#11.

Try these messages after logging in (stub responses until later phases):
  - "show me the gantt chart"
  - "moderate issue 42"  (or any number)
  - "what's the budget?"
  - "pause the loop"
  - "scope to 42, 43"
  - "reset setup"

To re-run the Setup Agent from scratch, delete .imp/config.json and refresh.
To also wipe the admin password, delete the file outright — `reset setup`
preserves the hash.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
from pathlib import Path

import chainlit as cl
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / ".imp" / "config.json"

_hasher = PasswordHasher()


# ---------- git / gh helpers ----------


def detect_repo_from_git() -> str | None:
    """Return `owner/name` from the local git origin, or None.

    Imp is expected to live inside the repo it manages: you `git clone`
    Imp into your project (or vendor it with git subtree), `cd` into the
    project root, and run `python imp/imp.py`. From there, the local git
    origin IS the target repo — no need to ask the admin which one.

    Looks at the current working directory's `git remote get-url origin`.
    Parses both SSH (`git@github.com:foo/bar.git`) and HTTPS
    (`https://github.com/foo/bar.git`) forms.
    """
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    url = result.stdout.strip()
    m = re.match(
        r"(?:git@github\.com:|https://github\.com/)([^/]+/[^/]+?)(?:\.git)?/?$",
        url,
    )
    return m.group(1) if m else None


async def gh_auth_status() -> tuple[bool, str]:
    """Return `(authenticated, combined_output)` from `gh auth status`."""
    proc = await asyncio.create_subprocess_exec(
        "gh",
        "auth",
        "status",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return proc.returncode == 0, out.decode().strip()


async def gh_repo_view(owner_repo: str) -> tuple[bool, str]:
    """Return `(exists, combined_output)` from `gh repo view`."""
    proc = await asyncio.create_subprocess_exec(
        "gh",
        "repo",
        "view",
        owner_repo,
        "--json",
        "nameWithOwner,defaultBranchRef,visibility",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return proc.returncode == 0, out.decode().strip()


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


# ---------- password verify ----------


def verify_password(plain: str, hashed: str) -> bool:
    """Return True if plain matches the stored argon2 hash."""
    try:
        _hasher.verify(hashed, plain)
        return True
    except VerifyMismatchError:
        return False


# ---------- auth ----------


@cl.password_auth_callback
def auth(username: str, password: str) -> cl.User | None:
    """Single-admin auth.

    `imp.py` seeds `admin_password_hash` into `.imp/config.json` on very
    first run via a terminal `getpass` prompt, so by the time Chainlit
    starts the hash is always present. If for some reason it isn't
    (config got deleted or corrupted), fail closed — we never let the
    user in without a verified password.

    Username is ignored; this is a single-admin deployment.
    """
    cfg = load_config()
    hashed = cfg.get("admin_password_hash")
    if not hashed:
        return None
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


# ---------- setup agent ----------


async def run_setup_agent() -> None:
    """Real setup flow, kept strictly to what's actually implementable at P1.2.

    Steps:
      1. Check `gh auth status`. If not authenticated, tell the admin to run
         `gh auth login --web` in a terminal and wait for them to say "ready".
      2. Auto-detect the target repo from `git remote get-url origin`.
         Imp is expected to live inside the repo it manages, so the local
         git context IS the config.
      3. Verify the repo is accessible via `gh repo view`.
      4. Save `{repo, setup_complete=true}` to config and hand off to Foreman.

    No fake project-board bootstrap step — that's KKallas/Imp#10. No fake
    loop configuration — that's KKallas/Imp#23. The wizard stops the
    moment the next step would be bogus.
    """
    await cl.Message(
        author="Setup Agent",
        content=(
            "Hi — I'm the **Setup Agent**. I'll do the real checks I can do "
            "right now and stop before anything that isn't implemented yet."
        ),
    ).send()

    # --- Step 1: gh auth status ---
    async with cl.Step(name="gh auth status", type="tool") as step:
        authed, status = await gh_auth_status()
        step.input = "gh auth status"
        step.output = status or "(no output)"

    if not authed:
        await cl.Message(
            author="Setup Agent",
            content=(
                "Your local `gh` CLI isn't authenticated. Open a terminal and run:\n\n"
                "```\ngh auth login --web\n```\n\n"
                "Follow the device-code flow in your browser. When `gh auth "
                "status` reports you're logged in, come back here and type "
                "*ready*."
            ),
        ).send()

        ready = await cl.AskUserMessage(
            content="Type *ready* once you've completed `gh auth login`:",
            timeout=600,
        ).send()
        if not ready or "ready" not in (ready.get("output") or "").lower():
            await cl.Message(
                author="Setup Agent",
                content=(
                    "No confirmation received. Say *retry setup* when you're "
                    "ready to continue, or complete `gh auth login` and refresh."
                ),
            ).send()
            return

        async with cl.Step(name="gh auth status (retry)", type="tool") as step:
            authed, status = await gh_auth_status()
            step.input = "gh auth status"
            step.output = status or "(no output)"

        if not authed:
            await cl.Message(
                author="Setup Agent",
                content=(
                    "Still not authenticated. Stopping here — fix the `gh` "
                    "setup and re-run `python imp.py` or say *retry setup*."
                ),
            ).send()
            return

    # --- Step 2: detect the target repo ---
    detected = detect_repo_from_git()
    if detected:
        await cl.Message(
            author="Setup Agent",
            content=(
                f"This directory's git origin points at **`{detected}`**. "
                f"Imp lives inside the repo it manages, so that's the target."
            ),
        ).send()
        repo = detected
    else:
        await cl.Message(
            author="Setup Agent",
            content=(
                "This directory doesn't look like a git repo with a GitHub "
                "remote. I'll need you to tell me manually which repo to manage."
            ),
        ).send()
        repo_msg = await cl.AskUserMessage(
            content="Type the repo as `owner/name`:",
            timeout=300,
        ).send()
        if not repo_msg:
            return
        repo = (repo_msg.get("output") or "").strip()
        if not re.match(r"^[^/\s]+/[^/\s]+$", repo):
            await cl.Message(
                author="Setup Agent",
                content=(
                    f"`{repo}` doesn't look like `owner/name`. Stopping — say "
                    f"*retry setup* when you're ready to try again."
                ),
            ).send()
            return

    # --- Step 3: verify the repo via gh ---
    async with cl.Step(name=f"gh repo view {repo}", type="tool") as step:
        exists, info = await gh_repo_view(repo)
        step.input = f"gh repo view {repo} --json nameWithOwner,defaultBranchRef,visibility"
        step.output = info or "(no output)"

    if not exists:
        await cl.Message(
            author="Setup Agent",
            content=(
                f"I couldn't access `{repo}` via `gh`. Check the repo exists "
                f"and your `gh` token has read access. Stopping here."
            ),
        ).send()
        return

    # --- Step 4: save and hand off ---
    cfg = load_config()
    cfg.update({"repo": repo, "setup_complete": True})
    save_config(cfg)

    await cl.Message(
        author="Setup Agent",
        content=(
            f"✅ Verified access to `{repo}` and saved to `.imp/config.json`.\n\n"
            f"That's everything I can do for real right now. The project-"
            f"board bootstrap (KKallas/Imp#10), loop configuration "
            f"(KKallas/Imp#23), and real Foreman worker (KKallas/Imp#11) land "
            f"in later phases.\n\n"
            f"Handing off to **Foreman** — the chat commands below are still "
            f"stub demos for now, but they show the shape of the real UX."
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
