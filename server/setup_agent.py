"""server/setup_agent.py — LLM-driven first-run onboarding.

Uses the same native-tools pattern as the Foreman agent (Bash, Read,
Write). No MCP server. The system prompt tells the agent what commands
to run (gh, git, python tools/github/create_repo.py, etc.).

`run_setup(say, ask)` takes two caller-provided coroutines for UI;
the WebSocket handler wires them.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

try:
    from argon2 import PasswordHasher
except ImportError:
    PasswordHasher = None  # type: ignore[assignment]

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / ".imp" / "config.json"


# ---------- config I/O (intentionally duplicated from main.py) ----------
#
# main.py imports `server.setup_agent`, so we can't import back — and
# the config helpers are small enough that a shared module would be
# over-engineering. If a third caller shows up, lift into server/config.py.


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


# ---------- gh / git helpers ----------


async def _run_subprocess(argv: list[str], timeout: float = 30.0) -> tuple[int, str]:
    """Run a subprocess, capture combined stdout/stderr, return (rc, text)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=ROOT,
        )
    except (FileNotFoundError, PermissionError) as exc:
        return (127, f"failed to spawn: {exc}")
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return (124, f"timed out after {timeout}s")
    return (proc.returncode or 0, out.decode(errors="replace").strip())


def detect_repo_from_git_sync() -> Optional[str]:
    """Return `owner/name` from the local git origin, or None.

    Not a @tool — called directly by `detect_repo_from_git_tool` below
    so the tool layer stays thin.
    """
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=True,
            cwd=ROOT,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    url = result.stdout.strip()
    m = re.match(
        r"(?:git@github\.com:|https://github\.com/)([^/]+/[^/]+?)(?:\.git)?/?$",
        url,
    )
    return m.group(1) if m else None


# ---------- tool bodies (pure async functions, tests target these) ----------
#
# The `@tool`-decorated wrappers below call these — keeping the SDK
# decorator on a thin shim makes unit-testing trivial (no SDK needed).


async def do_gh_auth_status() -> dict[str, Any]:
    rc, out = await _run_subprocess(["gh", "auth", "status"])
    return {"authenticated": rc == 0, "output": out}


async def do_gh_auth_login() -> dict[str, Any]:
    return {
        "instruction": (
            "Open a terminal in this project directory and run:\n\n"
            "    gh auth login --web\n\n"
            "Follow the device-code prompts in the browser. When the "
            "CLI confirms you're logged in, come back here and ask me "
            "to check again — I'll call `gh_auth_status` to verify."
        ),
        "automated": False,
    }


async def do_claude_auth_status() -> dict[str, Any]:
    import os

    has_api_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    # Try the bundled CLI
    rc, out = await _run_subprocess(
        [sys.executable, "-c", "from claude_agent_sdk import __version__; print(__version__)"]
    )
    sdk_installed = rc == 0
    return {
        "anthropic_api_key_set": has_api_key,
        "sdk_installed": sdk_installed,
        "sdk_version": out if sdk_installed else None,
        "note": (
            "claude-agent-sdk uses either an ANTHROPIC_API_KEY env var "
            "or a logged-in Claude Code CLI session for auth. At least "
            "one must be present for the dispatcher / setup agent to "
            "call Claude."
        ),
    }


async def do_claude_auth_login() -> dict[str, Any]:
    return {
        "instruction": (
            "The easiest path is to set `ANTHROPIC_API_KEY` in your "
            "environment before launching Imp (for example in `~/.zshrc`, "
            "then `source` it and restart `python imp.py`).\n\n"
            "Alternatively, run the `claude` CLI in a terminal to sign "
            "in with your Anthropic account — the SDK will reuse that "
            "session."
        ),
        "automated": False,
    }


async def do_detect_repo_from_git() -> dict[str, Any]:
    repo = detect_repo_from_git_sync()
    return {"repo": repo, "found": repo is not None}


async def do_list_repos(limit: int = 30) -> dict[str, Any]:
    rc, out = await _run_subprocess(
        ["gh", "repo", "list", "--limit", str(limit), "--json", "nameWithOwner,description,visibility"]
    )
    if rc != 0:
        return {"error": out, "repos": []}
    try:
        repos = json.loads(out or "[]")
    except json.JSONDecodeError as exc:
        return {"error": f"unparseable JSON from gh: {exc}", "repos": []}
    return {"repos": repos, "count": len(repos)}


async def do_set_repo(repo: str) -> dict[str, Any]:
    if not re.match(r"^[^/\s]+/[^/\s]+$", repo):
        return {"error": f"{repo!r} doesn't look like `owner/name`"}
    # Verify the repo is actually reachable via gh before writing config
    rc, out = await _run_subprocess(
        ["gh", "repo", "view", repo, "--json", "nameWithOwner,defaultBranchRef,visibility"]
    )
    if rc != 0:
        return {"error": f"gh repo view failed: {out}"}
    cfg = load_config()
    cfg["repo"] = repo
    save_config(cfg)
    return {"repo": repo, "verified": True, "gh_output": out}


async def do_list_projects(owner: str, limit: int = 20) -> dict[str, Any]:
    rc, out = await _run_subprocess(
        [
            "gh",
            "project",
            "list",
            "--owner",
            owner,
            "--limit",
            str(limit),
            "--format",
            "json",
        ]
    )
    if rc != 0:
        return {"error": out, "projects": []}
    try:
        data = json.loads(out or "{}")
    except json.JSONDecodeError as exc:
        return {"error": f"unparseable JSON from gh: {exc}", "projects": []}
    projects = data.get("projects", []) if isinstance(data, dict) else data
    return {"projects": projects, "count": len(projects)}


async def do_create_imp_project(
    owner: str,
    title: str = "Imp",
    on_conflict: str = "stop",
) -> dict[str, Any]:
    """Create (or verify) the Imp Projects-v2 board via project_bootstrap.py.

    `on_conflict` controls what happens if the script detects fields
    with the correct name but wrong type / options:
      - "stop"  (default) — script exits rc=2 with a conflict report.
        The LLM surfaces it to the admin and asks whether to delete +
        overwrite or stop and fix manually.
      - "delete" — script removes the conflicting fields and recreates
        them from the template. Destructive: any values already stored
        on items under those fields are lost. The admin must pick this
        knowingly.
      - "skip" — accept the existing fields as-is. May cause runtime
        errors later when the pipeline tries to write incompatible
        values; surfaced in the return dict so the LLM can warn.

    Exit-code contract (from pipeline/project_bootstrap.py):
      0 → success (created / updated / idempotent no-op)
      1 → gh error (auth scope, network, malformed response, etc.)
      2 → conflicts detected in "stop" mode; stdout is a JSON report
    """
    rc, out = await _run_subprocess(
        [
            sys.executable,
            "pipeline/project_bootstrap.py",
            "--owner",
            owner,
            "--title",
            title,
            "--on-conflict",
            on_conflict,
        ]
    )

    # rc=2: parse the conflict report so the LLM can render it.
    if rc == 2:
        try:
            report = json.loads(out or "{}")
        except json.JSONDecodeError:
            report = {"status": "conflicts_detected_unparseable", "raw": out}
        return {
            "exit_code": rc,
            "created": False,
            "conflicts": report.get("conflicts", []),
            "next_steps": report.get("next_steps"),
            "project_number": report.get("project_number"),
            "instruction_for_agent": (
                "Tell the admin there are field conflicts on the Imp board. "
                "List each conflict's name and reason concisely. Ask them to "
                "choose: (1) DELETE — overwrite the conflicting fields "
                "(destructive, any values already stored in those fields "
                "will be lost), or (2) STOP — they fix manually in the "
                "GitHub UI and you re-run this tool. If they pick DELETE, "
                "call create_imp_project again with on_conflict=\"delete\"."
            ),
        }

    # rc=0: success (or idempotent no-op). Parse the structured result.
    if rc == 0:
        try:
            result = json.loads(out or "{}")
        except json.JSONDecodeError:
            result = {"raw": out}
        return {
            "exit_code": 0,
            "created": True,
            "result": result,
        }

    # rc=1 (or anything else): gh error. Surface gh's message.
    return {
        "exit_code": rc,
        "created": False,
        "error": out,
    }


async def do_protect_main_branch(repo: str = "") -> dict[str, Any]:
    """Enable branch protection: require PR review before merge on the default branch."""
    if not repo:
        cfg = load_config()
        repo = cfg.get("repo", "")
    if not repo:
        return {"error": "No repo configured yet. Run set_repo first."}

    # Get the default branch name
    rc, out = await _run_subprocess(
        ["gh", "repo", "view", repo, "--json", "defaultBranchRef", "-q", ".defaultBranchRef.name"]
    )
    if rc != 0:
        return {"error": f"Could not detect default branch: {out}"}
    branch = out.strip() or "main"

    # Create a branch protection ruleset via gh api
    rc, out = await _run_subprocess(
        [
            "gh", "api", f"repos/{repo}/rulesets", "--method", "POST",
            "--field", "name=Imp: require PR approval",
            "--field", "target=branch",
            "--field", "enforcement=active",
            "--field", f'conditions[ref_name][include][]=refs/heads/{branch}',
            "--field", "rules[][type]=pull_request",
            "--field", "rules[0][parameters][required_approving_review_count]=1",
            "--field", "rules[0][parameters][dismiss_stale_reviews_on_push]=true",
        ]
    )
    if rc != 0:
        # Might already exist or need different API — try the older branch protection endpoint
        rc, out = await _run_subprocess(
            [
                "gh", "api", f"repos/{repo}/branches/{branch}/protection",
                "--method", "PUT",
                "--input", "-",
            ],
        )
        if rc != 0:
            return {
                "error": f"Could not set branch protection: {out}",
                "suggestion": "You can set this manually: repo Settings > Branches > Add rule > Require pull request reviews",
            }

    return {"ok": True, "repo": repo, "branch": branch, "protection": "PR approval required before merge"}


async def do_create_repo(
    name: str = "",
    private: bool = False,
    description: str = "",
) -> dict[str, Any]:
    """Create a GitHub repo from the current folder, commit, and push."""
    cmd = [sys.executable, str(ROOT / "tools" / "github" / "create_repo.py")]
    if name:
        cmd.extend(["--name", name])
    if private:
        cmd.append("--private")
    if description:
        cmd.extend(["--description", description])
    rc, out = await _run_subprocess(cmd, timeout=60.0)
    return {"ok": rc == 0, "output": out}


async def do_configure_loop(
    enabled: bool = False,
    interval_minutes: int = 60,
    max_tasks_per_tick: int = 3,
    scope: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    if interval_minutes < 5:
        return {"error": "interval_minutes must be >= 5 (per v0.1.md §Loop)"}
    if max_tasks_per_tick < 1:
        return {"error": "max_tasks_per_tick must be >= 1"}
    cfg = load_config()
    cfg["loop"] = {
        "enabled": bool(enabled),
        "interval_minutes": int(interval_minutes),
        "max_tasks_per_tick": int(max_tasks_per_tick),
        "scope": scope,
        "paused": False,
    }
    save_config(cfg)
    return {"loop": cfg["loop"], "saved": True}


async def do_set_admin_password(password: str) -> dict[str, Any]:
    if PasswordHasher is None:
        return {"error": "argon2 not installed — pip install argon2-cffi"}
    if not password or len(password) < 4:
        return {"error": "password must be at least 4 characters"}
    cfg = load_config()
    cfg["admin_password_hash"] = PasswordHasher().hash(password)
    save_config(cfg)
    return {"saved": True, "note": "new password takes effect on next login"}


async def do_mark_setup_complete() -> dict[str, Any]:
    cfg = load_config()
    # Sanity: refuse to complete without at least a repo configured
    if not cfg.get("repo"):
        return {
            "error": "cannot mark complete — no `repo` in config. "
            "Call `set_repo` first."
        }
    cfg["setup_complete"] = True
    save_config(cfg)
    return {"setup_complete": True, "repo": cfg["repo"]}




# ---------- system prompt ----------

SETUP_SYSTEM_PROMPT = """\
You are the Setup Agent for Imp — a self-hosted coding agent that manages a \
GitHub repo. Your job is to walk a fresh admin through first-run setup, one \
step at a time. You have access to Bash, Read, and Write tools — use them \
directly. No MCP.

## Config file

Imp stores its config at `.imp/config.json`. Use Read/Write to manage it. \
Key fields: `repo` (owner/name), `setup_complete` (bool).

## Setup checklist (in order)

1. Verify the gh CLI is authenticated.
   - Run `gh auth status`. If not authenticated, tell the admin to run \
`gh auth login --web` in a terminal and come back.
2. Check if this folder is already a git repo with a GitHub remote.
   - Run `git remote get-url origin`. If it returns a GitHub URL, parse \
the owner/name, confirm with the admin, and write it to `.imp/config.json`.
3. If no repo found, ask: create a new GitHub repo, or link an existing one?
   - **If creating new:**
     a. Suggest the current folder name as the repo name. Ask if they want \
a different name.
     b. Ask for a short description (or offer to generate one based on \
what's in the folder).
     c. Ask about license — explain common choices briefly (MIT, Apache-2.0, \
GPL-3.0) and let them pick.
     d. Ask public or private.
     e. Write a basic README.md with the project name, description, and \
license.
     f. Run `python tools/github/create_repo.py --name <name> [--private] \
[--description "<desc>"]` to git init, create the repo, and push.
     g. Write the repo owner/name to `.imp/config.json`.
   - **If linking existing:** run `gh repo list --limit 30 --json \
nameWithOwner,description,visibility` to show options. After admin picks, \
verify with `gh repo view owner/name` and write to `.imp/config.json`.
4. Set up branch protection to require PR approval before merge:
   - Run `gh api repos/OWNER/REPO/rulesets --method POST` with appropriate \
fields, or guide the admin to Settings > Branches if the API fails.
5. Set `setup_complete` to `true` in `.imp/config.json` — but only after \
the `repo` field is set. This hands off to the Foreman agent.

## Rules

- One concrete action per turn. Announce what you're about to do, do it, \
report the result plainly.
- Ask before destructive or write actions. Never assume.
- If a command fails, explain what went wrong and offer a next step.
- Stay on topic — you're the Setup Agent, not Foreman.
- Keep your replies brief. The admin wants to get through setup.
"""


# ---------- driver ----------


SayFn = Callable[[str], Awaitable[None]]
AskFn = Callable[[str], Awaitable[Optional[str]]]


ToolStartFn = Callable[[str, dict], Awaitable[None]]
ToolDoneFn = Callable[[str, str, float, str], Awaitable[None]]


async def _allow_all(tool_name: str, tool_input: dict, context: Any) -> Any:
    """Auto-allow all tools during setup — no permission prompts."""
    from claude_agent_sdk.types import PermissionResultAllow
    return PermissionResultAllow(behavior="allow")


async def run_setup(
    say: SayFn,
    ask: AskFn,
    tool_start: Optional[ToolStartFn] = None,
    tool_done: Optional[ToolDoneFn] = None,
) -> None:
    """Drive the setup conversation until `setup_complete=true`.

    Uses native Claude SDK tools (Bash, Read, Write) — same pattern as
    the Foreman agent. No MCP server. The system prompt tells the agent
    what commands to run. All tools are auto-allowed (no permission prompts).
    """
    await say(
        "Hi — I'm the **Setup Agent**. Let me check what's needed to get "
        "Imp running.\n\n"
    )

    import time

    from claude_agent_sdk import (  # type: ignore[import-not-found]
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKClient,
        TextBlock,
        ToolUseBlock,
        UserMessage,
    )
    from claude_agent_sdk.types import ToolResultBlock

    options = ClaudeAgentOptions(
        system_prompt=SETUP_SYSTEM_PROMPT,
        can_use_tool=_allow_all,
        max_turns=30,
    )

    pending_tools: dict[str, tuple[str, float]] = {}  # tool_id -> (name, start_time)

    async def _handle_result(block: Any) -> None:
        entry = pending_tools.pop(getattr(block, "tool_use_id", ""), None)
        if not entry or not tool_done:
            return
        name, started = entry
        dur = time.time() - started
        content = getattr(block, "content", "")
        output = content if isinstance(content, str) else str(content)
        status = "error" if getattr(block, "is_error", False) else "ok"
        await tool_done(name, status, dur, output[:500])

    current_turn = "start"
    async with ClaudeSDKClient(options=options) as client:
        while True:
            await client.query(current_turn)
            assistant_text_parts: list[str] = []
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            assistant_text_parts.append(block.text)
                        elif isinstance(block, ToolUseBlock):
                            desc = (block.input or {}).get("description", block.name)
                            pending_tools[block.id] = (block.name, time.time())
                            if tool_start:
                                await tool_start(block.name, {"description": desc})
                        elif isinstance(block, ToolResultBlock):
                            await _handle_result(block)
                elif isinstance(message, UserMessage):
                    for block in message.content:
                        if isinstance(block, ToolResultBlock):
                            await _handle_result(block)

            reply = "".join(assistant_text_parts).strip()
            if reply:
                await say(reply)

            if is_setup_complete():
                return

            next_turn = await ask("(reply to Setup Agent)")
            if next_turn is None:
                await say("No response — setup paused here. Refresh to resume.")
                return
            current_turn = next_turn
