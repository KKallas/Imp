"""server/setup_agent.py — LLM-driven first-run onboarding.

Distinct persona from the Foreman dispatcher. Narrow toolset scoped to
the four things a fresh Imp install needs before Foreman can do real
work:

  1. Ensure `gh` CLI is authenticated.
  2. Pick the target repo (auto-detect from git, or user provides).
  3. (Later: create an Imp Projects-v2 board — blocked on KKallas/Imp#10.)
  4. (Later: configure the autonomous loop — blocked on KKallas/Imp#23.)
  5. Mark setup complete and hand off to Foreman.

Unlike the Foreman dispatcher (server/dispatcher.py), the Setup Agent
uses **claude-agent-sdk's native tool-calling**: tools are real Python
functions registered via `create_sdk_mcp_server`, and the LLM invokes
them through the SDK's MCP pipeline. This matches v0.1.md §Setup Agent
and is the right shape when the agent is supposed to do several
concrete, independently-testable actions in sequence.

## Tools

Fully implemented:
  - `gh_auth_status` — check `gh auth status`
  - `detect_repo_from_git` — parse `git remote get-url origin`
  - `list_repos` — `gh repo list` with access
  - `list_projects` — `gh project list --owner`
  - `set_repo` — write the chosen repo to `.imp/config.json`
  - `set_admin_password` — update the argon2 hash in config
  - `configure_loop` — persist loop settings (no loop runner yet)
  - `mark_setup_complete` — flip setup_complete=true

Pragmatic (returns user-instruction message, no automation):
  - `gh_auth_login` — instructs the admin to run `gh auth login --web`
    in a terminal. Full device-flow polling is a follow-up.
  - `claude_auth_status` / `claude_auth_login` — report env-var /
    bundled CLI state and tell the admin how to fix it.

Stub (blocked on another issue):
  - `create_imp_project` — calls `pipeline/project_bootstrap.py`,
    currently a stub that returns a "KKallas/Imp#10 not merged yet"
    error.

## No UI import

The module has no UI import. `run_setup(say, ask)` takes two
caller-provided coroutines for UI; the WebSocket handler wires them.
Tools themselves return structured dicts — they never talk to the user
directly; the LLM decides what to say.
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


# ---------- @tool wrappers (used only when the SDK is available) ----------
#
# Wrapped lazily inside `_build_mcp_server()` so modules that import
# `server.setup_agent` but don't run the LLM (tests, type-checking)
# don't need the SDK installed.


def _build_mcp_server() -> Any:
    """Create the MCP server with every setup tool registered.

    Called once per `run_setup()` — rebuilds on every session so the
    tools always see fresh config.
    """
    from claude_agent_sdk import create_sdk_mcp_server, tool

    @tool("gh_auth_status", "Check if the gh CLI is authenticated.", {})
    async def gh_auth_status_tool(args: dict[str, Any]) -> dict[str, Any]:
        res = await do_gh_auth_status()
        return {"content": [{"type": "text", "text": json.dumps(res)}]}

    @tool(
        "gh_auth_login",
        "Start the gh device-code flow. Returns a user instruction — the admin "
        "completes the login in a terminal, then asks you to verify via gh_auth_status.",
        {},
    )
    async def gh_auth_login_tool(args: dict[str, Any]) -> dict[str, Any]:
        res = await do_gh_auth_login()
        return {"content": [{"type": "text", "text": json.dumps(res)}]}

    @tool(
        "claude_auth_status",
        "Check if claude-agent-sdk has usable credentials (API key or CLI).",
        {},
    )
    async def claude_auth_status_tool(args: dict[str, Any]) -> dict[str, Any]:
        res = await do_claude_auth_status()
        return {"content": [{"type": "text", "text": json.dumps(res)}]}

    @tool(
        "claude_auth_login",
        "Returns instructions for setting ANTHROPIC_API_KEY or logging into the claude CLI.",
        {},
    )
    async def claude_auth_login_tool(args: dict[str, Any]) -> dict[str, Any]:
        res = await do_claude_auth_login()
        return {"content": [{"type": "text", "text": json.dumps(res)}]}

    @tool(
        "detect_repo_from_git",
        "Return owner/name from the local git remote origin, or null.",
        {},
    )
    async def detect_repo_tool(args: dict[str, Any]) -> dict[str, Any]:
        res = await do_detect_repo_from_git()
        return {"content": [{"type": "text", "text": json.dumps(res)}]}

    @tool(
        "list_repos",
        "List GitHub repos the user can access via gh.",
        {"limit": int},
    )
    async def list_repos_tool(args: dict[str, Any]) -> dict[str, Any]:
        res = await do_list_repos(int(args.get("limit", 30)))
        return {"content": [{"type": "text", "text": json.dumps(res)}]}

    @tool(
        "create_repo",
        "Create a new GitHub repo from the current folder. Runs git init, "
        "gh repo create, commits all files, and pushes. Use this when "
        "detect_repo_from_git finds no repo and the admin wants to create "
        "a new one instead of linking an existing one.",
        {"name": str, "private": bool, "description": str},
    )
    async def create_repo_tool(args: dict[str, Any]) -> dict[str, Any]:
        res = await do_create_repo(
            name=str(args.get("name", "")),
            private=bool(args.get("private", False)),
            description=str(args.get("description", "")),
        )
        return {"content": [{"type": "text", "text": json.dumps(res)}]}

    @tool(
        "set_repo",
        "Write the target repo (owner/name) to .imp/config.json after verifying "
        "access via `gh repo view`.",
        {"repo": str},
    )
    async def set_repo_tool(args: dict[str, Any]) -> dict[str, Any]:
        res = await do_set_repo(str(args["repo"]))
        return {"content": [{"type": "text", "text": json.dumps(res)}]}

    @tool(
        "list_projects",
        "List Projects v2 boards owned by `owner`.",
        {"owner": str, "limit": int},
    )
    async def list_projects_tool(args: dict[str, Any]) -> dict[str, Any]:
        res = await do_list_projects(
            owner=str(args["owner"]), limit=int(args.get("limit", 20))
        )
        return {"content": [{"type": "text", "text": json.dumps(res)}]}

    @tool(
        "create_imp_project",
        "Provision (or verify) the Imp Projects-v2 board and its seven custom "
        "fields. Idempotent. If the board already exists with a field whose "
        "type or options differ from the template, the tool returns a "
        "`conflicts` list by default — ASK the admin whether to DELETE "
        "(overwrite, destructive) or STOP (they fix manually). On a DELETE "
        "choice, call this tool again with on_conflict=\"delete\". "
        "Valid on_conflict values: stop (default), delete, skip.",
        {"owner": str, "title": str, "on_conflict": str},
    )
    async def create_project_tool(args: dict[str, Any]) -> dict[str, Any]:
        res = await do_create_imp_project(
            owner=str(args["owner"]),
            title=str(args.get("title", "Imp")),
            on_conflict=str(args.get("on_conflict", "stop")),
        )
        return {"content": [{"type": "text", "text": json.dumps(res)}]}

    @tool(
        "configure_loop",
        "Persist autonomous-loop settings. Validates interval_minutes >= 5.",
        {"enabled": bool, "interval_minutes": int, "max_tasks_per_tick": int},
    )
    async def configure_loop_tool(args: dict[str, Any]) -> dict[str, Any]:
        res = await do_configure_loop(
            enabled=bool(args.get("enabled", False)),
            interval_minutes=int(args.get("interval_minutes", 60)),
            max_tasks_per_tick=int(args.get("max_tasks_per_tick", 3)),
        )
        return {"content": [{"type": "text", "text": json.dumps(res)}]}

    @tool(
        "set_admin_password",
        "Update the argon2id hash for the admin login password.",
        {"password": str},
    )
    async def set_password_tool(args: dict[str, Any]) -> dict[str, Any]:
        res = await do_set_admin_password(str(args["password"]))
        return {"content": [{"type": "text", "text": json.dumps(res)}]}

    @tool(
        "mark_setup_complete",
        "Flip setup_complete=true so the next chat session hands off to Foreman. "
        "Refuses if the repo isn't set yet.",
        {},
    )
    async def mark_complete_tool(args: dict[str, Any]) -> dict[str, Any]:
        res = await do_mark_setup_complete()
        return {"content": [{"type": "text", "text": json.dumps(res)}]}

    return create_sdk_mcp_server(
        name="imp_setup",
        tools=[
            gh_auth_status_tool,
            gh_auth_login_tool,
            claude_auth_status_tool,
            claude_auth_login_tool,
            detect_repo_tool,
            create_repo_tool,
            list_repos_tool,
            set_repo_tool,
            list_projects_tool,
            create_project_tool,
            configure_loop_tool,
            set_password_tool,
            mark_complete_tool,
        ],
    )


# ---------- system prompt ----------

SETUP_SYSTEM_PROMPT = """\
You are the Setup Agent for Imp — a self-hosted coding agent that manages a \
GitHub repo. Your job is to walk a fresh admin through first-run setup, one \
step at a time, calling the provided tools to make changes and only acting on \
what the admin explicitly agrees to.

## Setup checklist (in order)

1. Verify the gh CLI is authenticated.
   - Call `gh_auth_status`. If not authenticated, call `gh_auth_login` to get \
the instruction text, show it to the admin, and wait for them to come back \
before calling `gh_auth_status` again.
2. Confirm Claude SDK auth is present.
   - Call `claude_auth_status`. If not usable, call `claude_auth_login` for \
guidance and surface it.
3. Pick the target repo.
   - Call `detect_repo_from_git`. If a repo comes back, confirm with the \
admin before calling `set_repo`.
   - If no repo found, ask: create a new GitHub repo, or link an existing one?
   - **If creating new:**
     a. Suggest the current folder name as the repo name. Ask if they want \
a different name.
     b. Ask for a short description (or offer to generate one based on \
what's in the folder).
     c. Ask about license — explain common choices briefly (MIT, Apache-2.0, \
GPL-3.0) and let them pick. The user can ask questions to make an \
informed choice.
     d. Ask public or private.
     e. Generate a basic README.md with the project name, description, and \
license before creating the repo.
     f. Call `create_repo` with the chosen name, visibility, and description. \
This will git init, commit everything, and push.
     g. After create_repo succeeds, call `set_repo` with the new repo name \
to save it in config.
   - **If linking existing:** list repos with `list_repos`, let admin choose, \
then call `set_repo`.
4. Provision or verify the Imp Projects-v2 board with `create_imp_project`. \
Idempotent — safe to run whether the board exists or not.
   - If the tool returns a `conflicts` list (same-named fields with the wrong \
type or different single-select options), describe each conflict plainly, \
then ASK the admin two choices: (a) DELETE and overwrite the conflicting \
fields — explain this is destructive and any existing values under those \
fields will be lost, or (b) STOP so they can fix the fields manually in the \
GitHub UI. If they pick DELETE, call `create_imp_project` again with \
`on_conflict="delete"`.
   - If it returns `error`, read gh's message and help the admin fix the \
underlying problem (usually `gh auth refresh -s project`).
5. (Optional) Configure the autonomous loop with `configure_loop` if the \
admin wants it on.
6. Call `mark_setup_complete` once the repo is set — this flips the flag so \
the next chat session hands off to Foreman.

## Rules

- One concrete action per turn. Announce what you're about to do, call the \
tool, report the result plainly.
- Ask before destructive or write actions. Never assume.
- If a tool returns an error, explain what went wrong in one or two sentences \
and offer a next step.
- Stay on topic — you're the Setup Agent, not Foreman. Don't volunteer to \
moderate issues or render gantt charts.
- Keep your replies brief. The admin wants to get through setup.
"""


# ---------- driver ----------


SayFn = Callable[[str], Awaitable[None]]
AskFn = Callable[[str], Awaitable[Optional[str]]]


async def run_setup(say: SayFn, ask: AskFn) -> None:
    """Drive the setup conversation until `setup_complete=true`.

    `say(text)` posts a Setup-Agent-authored message. `ask(question)`
    prompts the admin and returns their reply (or None on timeout).
    Caller is responsible for wiring those to the UI layer.
    """
    await say(
        "Hi — I'm the **Setup Agent**. I'll walk you through the checks Imp "
        "needs before Foreman can do real work. I'll only act with your "
        "confirmation. Say *ready* (or something like it) to begin."
    )

    first = await ask("Ready to start setup?")
    if first is None:
        await say("No response — setup paused. Refresh to try again.")
        return

    # Lazy SDK import so test / type-checker environments without the
    # SDK can still import this module.
    from claude_agent_sdk import (  # type: ignore[import-not-found]
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKClient,
        TextBlock,
        ToolUseBlock,
    )

    options = ClaudeAgentOptions(
        system_prompt=SETUP_SYSTEM_PROMPT,
        mcp_servers={"imp_setup": _build_mcp_server()},
        # Allow all our tools (mcp__<server>__<tool> is the conventional name)
        allowed_tools=[
            f"mcp__imp_setup__{t}"
            for t in (
                "gh_auth_status",
                "gh_auth_login",
                "claude_auth_status",
                "claude_auth_login",
                "detect_repo_from_git",
                "create_repo",
                "list_repos",
                "set_repo",
                "list_projects",
                "create_imp_project",
                "configure_loop",
                "set_admin_password",
                "mark_setup_complete",
            )
        ],
        max_turns=30,
    )

    current_turn = first
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
                            # Surface tool calls so the admin sees them
                            # even before the result text lands.
                            await say(
                                f"_Calling tool: `{block.name}`_"
                                + (
                                    f"\n```json\n{json.dumps(block.input, indent=2)}\n```"
                                    if block.input
                                    else ""
                                )
                            )

            reply = "".join(assistant_text_parts).strip()
            if reply:
                await say(reply)

            # Bail once the agent has set setup_complete — the next
            # chat session will pick up Foreman.
            if is_setup_complete():
                return

            next_turn = await ask("(reply to Setup Agent)")
            if next_turn is None:
                await say("No response — setup paused here. Refresh to resume.")
                return
            current_turn = next_turn
