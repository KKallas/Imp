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
from contextlib import asynccontextmanager
from pathlib import Path

import chainlit as cl
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from chainlit.input_widget import NumberInput, Select, Switch

from server import budgets, chat_history
from server.config import (
    load_config,
    save_config,
    is_setup_complete,
    detect_repo_from_git,
)

# ── render server (P4.24 renderer plugin system) ───────────────────
# Runs on a separate port (default 8421) so it has no auth middleware.
_RENDER_BASE_URL: str = ""
try:
    from server.render_route import start_background as _start_render_server

    _RENDER_BASE_URL = _start_render_server()
except Exception:
    pass  # render server unavailable — non-fatal

ROOT = Path(__file__).resolve().parent

_hasher = PasswordHasher()


# ---------- Chainlit data layer (KKallas/Imp#45) ----------
# Registers our JSON-backed data layer so Chainlit's native left sidebar
# shows past chats with click-to-resume, rename, and delete.


@cl.data_layer
def _imp_data_layer():
    from server.data_layer import ImpDataLayer

    return ImpDataLayer()


# ---------- git / gh helpers ----------


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


# ---------- admin budget panel + live status bar ----------
#
# The Budgets panel is the **only** way to change the three counters' limits
# or zero them out. Foreman never gets `set_*_budget` / `reset_budgets` in
# its tool list — a budget the agent can lift isn't a budget. The settings
# below render in Chainlit's gear-icon panel; the live bar is an updateable
# message pinned at the top of the chat that follows every intercept run.


def _progress_bar(used: int, limit: int, width: int = 20) -> str:
    if limit <= 0:
        return "░" * width
    pct = min(1.0, max(0.0, used / limit))
    filled = int(round(pct * width))
    return "█" * filled + "░" * (width - filled)


def _render_budget_status() -> str:
    """Markdown block with three progress bars — token / edit / task."""
    b = budgets.get_budgets()
    rows = []
    for label, used, limit in (
        ("Tokens", b.tokens_used, b.tokens_limit),
        ("Edits ", b.edits_used, b.edits_limit),
        ("Tasks ", b.tasks_used, b.tasks_limit),
    ):
        bar = _progress_bar(used, limit)
        pct = (used / limit * 100) if limit > 0 else 0
        rows.append(
            f"`{label} {bar} {used:>7,} / {limit:<7,} ({pct:5.1f}%)`"
        )
    return "**Budgets**\n\n" + "\n".join(rows)


def _is_admin() -> bool:
    """True if the current Chainlit session is allowed to change budgets.

    Imp is a single-role app: only the admin can log in at all (argon2
    hash in `.imp/config.json`). So "logged in" == "admin". We don't
    gate on `user.metadata["role"]` because Chainlit 2.x doesn't reliably
    surface the auth-callback metadata in `on_chat_start` / `on_settings_update`
    when the app has no persistence layer configured — the gate silently
    bailed, hid the gear icon, and the bar never rendered regardless of
    the toggle. Multi-role gating lands when the app grows more than one
    user.
    """
    return cl.user_session.get("user") is not None


async def register_budget_settings() -> None:
    """Render the admin Budgets panel behind Chainlit's gear icon.

    Six controls: three NumberInputs (limits) + three Switches (reset on
    save) + one Switch (show the live bar in chat). Initial values come
    from the on-disk state. Settings ARE the input shape; saving fires
    `@cl.on_settings_update` below.
    """
    import sys

    user = cl.user_session.get("user")
    print(f"[budget-settings] register called — user={user!r}", file=sys.stderr)
    if not _is_admin():
        print("[budget-settings] not admin — gear icon hidden", file=sys.stderr)
        return
    b = budgets.get_budgets()
    cfg = load_config()
    show_bar = bool(cfg.get("show_budget_bar", False))
    settings = await cl.ChatSettings(
        [
            NumberInput(
                id="token_limit",
                label="Token budget — limit",
                initial=b.tokens_limit,
                min=0,
                step=1000,
                tooltip="Claude API tokens (in + out) across every agent. Hard rejection at zero.",
            ),
            NumberInput(
                id="edit_limit",
                label="Edit budget — limit",
                initial=b.edits_limit,
                min=0,
                step=1,
                tooltip="Approved checkpoint-B writes to GitHub.",
            ),
            NumberInput(
                id="task_limit",
                label="Task budget — limit",
                initial=b.tasks_limit,
                min=0,
                step=1,
                tooltip="Pipeline-script invocations (moderate / solve / fix).",
            ),
            # Chainlit ChatSettings has no button / link widget, so the
            # reset action lives on a dropdown. Pick a counter (or "All"),
            # hit Confirm, it fires. Defaults back to "(none)" on next render.
            Select(
                id="reset_counter",
                label="Reset counter (on Confirm)",
                values=["(none)", "Tokens", "Edits", "Tasks", "All"],
                initial_value="(none)",
                tooltip="Pick a counter to zero out. The limit is not touched.",
            ),
            Switch(
                id="show_budget_bar",
                label="Show live budget bar in chat",
                initial=show_bar,
                tooltip="Auto-enables when you change any limit.",
            ),
        ]
    ).send()
    cl.user_session.set("settings", settings)


async def refresh_budget_bar() -> None:
    """Keep the live budget bar as the **last message** in the chat.

    Chainlit has no fixed status slot — a regular `cl.Message.update()`
    would refresh the content in-place but leave the bar wherever it
    was first drawn, scrolling out of view as new messages arrive. So
    on every turn we remove the old bar (if any) and send a fresh one
    at the current end of the transcript. The bar stays pinned just
    above the input box where the admin actually looks.

    Safe to call from `on_chat_start`, at the end of `@cl.on_message`
    (in a `finally`), after `run_demo_command`, and after
    `on_settings_update`. No-op when the admin has turned the bar off.
    """
    import sys

    if not _is_admin():
        print("[budget-bar] skipped: no user in session", file=sys.stderr)
        return
    cfg = load_config()
    show = cfg.get("show_budget_bar", False)
    print(
        f"[budget-bar] refresh called — show_budget_bar={show}",
        file=sys.stderr,
    )
    if not show:
        # Admin turned it off. If a bar was previously rendered, tidy it up.
        old = cl.user_session.get("budget_status_msg")
        if old is not None:
            try:
                await old.remove()
            except Exception as e:
                print(f"[budget-bar] remove failed: {e}", file=sys.stderr)
            cl.user_session.set("budget_status_msg", None)
        return

    old = cl.user_session.get("budget_status_msg")
    if old is not None:
        try:
            await old.remove()
        except Exception as e:
            print(f"[budget-bar] remove (replace) failed: {e}", file=sys.stderr)
    try:
        msg = cl.Message(author="Budgets", content=_render_budget_status())
        await msg.send()
        cl.user_session.set("budget_status_msg", msg)
        print("[budget-bar] sent new bar", file=sys.stderr)
    except Exception as e:
        print(f"[budget-bar] send failed: {type(e).__name__}: {e}", file=sys.stderr)


@cl.on_settings_update
async def on_settings_update(settings: dict) -> None:
    """Apply Budgets-panel changes. Admin-only.

    The agent never reaches this code path — the panel is the human's
    surface. We diff against the current limits to decide whether to
    auto-flip the live-bar toggle.
    """
    import sys

    print(f"[budget-settings] update fired: {settings}", file=sys.stderr)
    if not _is_admin():
        print("[budget-settings] not admin — bailing", file=sys.stderr)
        return

    before = budgets.get_budgets()

    # Apply limit changes (only when actually different — avoids spurious
    # state writes that look like activity)
    new_token = int(settings.get("token_limit", before.tokens_limit))
    new_edit = int(settings.get("edit_limit", before.edits_limit))
    new_task = int(settings.get("task_limit", before.tasks_limit))
    limit_changes: list[str] = []
    if new_token != before.tokens_limit:
        budgets.set_token_budget(new_token)
        limit_changes.append(f"tokens={new_token:,}")
    if new_edit != before.edits_limit:
        budgets.set_edit_budget(new_edit)
        limit_changes.append(f"edits={new_edit}")
    if new_task != before.tasks_limit:
        budgets.set_task_budget(new_task)
        limit_changes.append(f"tasks={new_task}")

    # Reset dropdown: "(none)" / "Tokens" / "Edits" / "Tasks" / "All".
    reset_choice = str(settings.get("reset_counter", "(none)"))
    reset_label: str | None = None
    if reset_choice == "Tokens":
        budgets.reset_counter("tokens")
        reset_label = "tokens"
    elif reset_choice == "Edits":
        budgets.reset_counter("edits")
        reset_label = "edits"
    elif reset_choice == "Tasks":
        budgets.reset_counter("tasks")
        reset_label = "tasks"
    elif reset_choice == "All":
        budgets.reset_all_counters()
        reset_label = "all"

    # Auto-flip the live bar on if the admin set a limit. Rationale:
    # the only reason to tighten a budget is because you want to *watch*
    # it — opting in to the constraint should opt you in to the readout.
    show_bar = bool(settings.get("show_budget_bar", False))
    auto_enabled = False
    if limit_changes and not show_bar:
        show_bar = True
        auto_enabled = True

    cfg = load_config()
    cfg["show_budget_bar"] = show_bar
    save_config(cfg)

    # Confirmation message — terse, not a popup
    bits: list[str] = []
    if limit_changes:
        bits.append("limits: " + ", ".join(limit_changes))
    if reset_label:
        bits.append(f"reset: {reset_label}")
    if auto_enabled:
        bits.append("live bar enabled (auto)")
    if not bits:
        bits.append("no changes")
    await cl.Message(
        author="Budgets",
        content="Confirmed — " + " · ".join(bits),
    ).send()

    # Re-render the settings panel so the reset dropdown snaps back to
    # "(none)" — it's a one-shot command, not a stored preference.
    if reset_label:
        await register_budget_settings()

    # Send / refresh the live bar to reflect the new state. Always ends
    # up as the most recent message so the admin can see it.
    await refresh_budget_bar()


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
        # P3.9: LLM-driven Setup Agent replaces the hardcoded wizard.
        # The old `run_setup_agent` is kept below as a reference / fallback
        # in case the SDK is unavailable, but the live path is the new one.
        from server import setup_agent

        try:
            await setup_agent.run_setup(
                say=_foreman_say_as("Setup Agent"),
                ask=_foreman_ask_as("Setup Agent"),
            )
        except Exception as exc:  # noqa: BLE001 — fall back cleanly if SDK is broken
            await cl.Message(
                author="Setup Agent",
                content=(
                    f"The LLM-driven setup agent failed to start ({exc}). "
                    f"Falling back to the hardcoded wizard."
                ),
            ).send()
            await run_setup_agent()
    else:
        await greet_foreman()
    await register_budget_settings()
    await refresh_budget_bar()


@cl.on_chat_resume
async def on_resume(thread: dict) -> None:
    """Restore a past chat session when the admin clicks it in the
    sidebar. Loads the ChatSession from disk (not from `thread`, which
    only has Chainlit's view of the steps) so we get our full Turn
    list with tool_calls and title_source."""
    thread_id = thread.get("id") or ""
    session = chat_history.load_session(thread_id)
    if session is None:
        # Thread exists in Chainlit's view but we have no JSON for it.
        # Create a fresh session so the rest of the handlers work.
        cfg = load_config()
        session = chat_history.ChatSession.new(
            repo=cfg.get("repo"), id=thread_id
        )
        session.rename(thread.get("name") or chat_history.FALLBACK_TITLE, by="fallback")
    cl.user_session.set("chat_session", session)
    await _render_chat_header(session, fresh=True)
    await register_budget_settings()
    await refresh_budget_bar()


def _foreman_say_as(author: str):
    """Build a `say` coroutine whose messages are attributed to `author`."""

    async def _say(text: str) -> None:
        await cl.Message(author=author, content=text).send()

    return _say


def _foreman_ask_as(author: str):
    """Build an `ask` coroutine that labels AskUserMessage with `author`."""

    async def _ask(question: str) -> str | None:
        resp = await cl.AskUserMessage(
            author=author,
            content=question,
            timeout=600,
        ).send()
        if not resp:
            return None
        answer = (resp.get("output") if isinstance(resp, dict) else None) or ""
        answer = answer.strip()
        return answer or None

    return _ask


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
    # KKallas/Imp#62: resume the most recent session on page load instead
    # of creating a new stub every time. The admin creates new chats
    # explicitly via /new or the "New Chat" button.
    await _resume_or_start_session()


# ---------- chat history (KKallas/Imp#45) ----------
#
# A `ChatSession` tracks the current in-memory turn list, its agent- or
# user-picked title, and the path on disk where it's persisted after
# every turn. The session lives in `cl.user_session` so each browser
# tab gets its own thread. The header message is a separate pinned
# `cl.Message` that shows the title + three action buttons; it's
# updated in-place on rename / new-chat / load so the admin always
# knows which chat they're in.


def _current_session() -> chat_history.ChatSession | None:
    return cl.user_session.get("chat_session")


async def _start_new_chat_session() -> chat_history.ChatSession:
    """Create a fresh session, save a stub file to disk, render the
    header. Any previously-active session is left on disk under its
    own filename — "new chat" means "rotate", not "discard".

    Uses Chainlit's `thread_id` as the session id so the data layer
    and the sidebar always agree on which thread is which.
    """
    cfg = load_config()
    # Use Chainlit's thread_id so the data layer's list_threads maps 1:1.
    thread_id: str | None = None
    try:
        thread_id = cl.context.session.thread_id
    except Exception:
        pass
    session = chat_history.ChatSession.new(
        repo=cfg.get("repo"), id=thread_id
    )
    cl.user_session.set("chat_session", session)
    chat_history.save_session(session)
    await _render_chat_header(session, fresh=True)
    return session


async def _resume_or_start_session() -> chat_history.ChatSession:
    """Resume the most recent chat, or create a new one if none exist.

    Distinguishes page-refresh from "New Chat" click by checking whether
    Chainlit's ``thread_id`` already has a session on disk:
      - **known thread_id** → page refresh → resume that session
      - **unknown thread_id + existing chats** → "New Chat" click → create new
      - **no chats at all** → first run → create new

    Also prunes empty stubs so the sidebar stays clean.
    """
    # Check if Chainlit gave us a thread_id that we already know about.
    thread_id: str | None = None
    try:
        thread_id = cl.context.session.thread_id
    except Exception:
        pass

    if thread_id:
        on_disk = chat_history.load_session(thread_id)
        if on_disk is not None:
            # Page refresh — resume this exact session.
            cl.user_session.set("chat_session", on_disk)
            await _render_chat_header(on_disk, fresh=True)
            chat_history.prune_stubs()
            return on_disk

    # thread_id is new (New Chat click) or no chats exist — create fresh.
    session = await _start_new_chat_session()
    # Prune AFTER creating, so the new stub is the "keep" and all
    # older empties are deleted.
    chat_history.prune_stubs()
    return session


async def _render_chat_header(
    session: chat_history.ChatSession, *, fresh: bool = False
) -> None:
    """Post (or update) the pinned chat-header message with the current
    title + action buttons. `fresh=True` posts a new message and stores
    a reference; subsequent updates edit the same message so the header
    doesn't multiply as titles change.
    """
    content = _format_header_content(session)
    actions = _chat_header_actions(session)

    existing: cl.Message | None = cl.user_session.get("chat_header_msg")
    if fresh or existing is None:
        msg = cl.Message(
            author="Foreman",
            content=content,
            actions=actions,
        )
        await msg.send()
        cl.user_session.set("chat_header_msg", msg)
        return

    existing.content = content
    # `actions` must be reset along with content; Chainlit doesn't diff
    # them but a fresh list drawn on update keeps the button state
    # consistent (e.g. "New chat" label never changes, but future
    # variants could).
    existing.actions = actions
    try:
        await existing.update()
    except Exception as exc:  # noqa: BLE001 — header updates are cosmetic
        import sys as _sys

        print(
            f"[chat-history] header update failed: "
            f"{type(exc).__name__}: {exc}",
            file=_sys.stderr,
        )


def _format_header_content(session: chat_history.ChatSession) -> str:
    badge = {
        "user": "✏️",  # manual rename — don't auto-overwrite
        "agent": "🤖",  # agent-titled
        "fallback": "•",  # no real title yet
    }.get(session.title_source, "•")
    return (
        f"**Chat:** {badge} {session.title}  \n"
        f"_id: `{session.id}` · turns: {len(session.turns)}_"
    )


def _chat_header_actions(session: chat_history.ChatSession) -> list[cl.Action]:
    # "Recent chats" is handled natively by Chainlit's left sidebar
    # (via our data layer), so it's omitted here. "New chat" is also
    # in the sidebar but duplicated here for discoverability.
    return [
        cl.Action(
            name="chat_new",
            payload={},
            label="🆕 New chat",
            tooltip="Archive this chat and start a fresh one.",
        ),
        cl.Action(
            name="chat_rename",
            payload={"session_id": session.id},
            label="✏️ Rename",
            tooltip="Rename this chat (locks it against agent re-titling).",
        ),
    ]


async def _handle_new_chat_command() -> None:
    """Archive the current session (already on disk) and start fresh.
    Shared by the `/new` text command and the header action button so
    both do exactly the same thing — the issue spec says they must."""
    old = _current_session()
    if old is not None:
        # Persist one last time before rotating so any in-memory changes
        # since the last turn (rename after typing /new, etc.) land.
        try:
            chat_history.save_session(old)
        except Exception as exc:  # noqa: BLE001
            import sys as _sys

            print(
                f"[chat-history] archive-on-rotate failed: "
                f"{type(exc).__name__}: {exc}",
                file=_sys.stderr,
            )
    await _start_new_chat_session()
    await cl.Message(
        author="Foreman",
        content=(
            "Started a new chat. The previous conversation is saved "
            "and visible in the sidebar."
        ),
    ).send()


async def _maybe_retitle_session(session: chat_history.ChatSession) -> None:
    """After the first real assistant reply, pick an agent title.

    Skips when the admin has manually renamed (title_source == "user")
    or when there aren't enough turns yet. Failures here are silent —
    a missing title is cosmetic, not a correctness bug.
    """
    if session.title_source == "user":
        return
    if not session.needs_agent_title():
        return
    # Only re-title on the transition from fallback → agent. Subsequent
    # topic-shift re-titling is out of scope for P4.20; admin can
    # manually rename.
    if session.title_source == "agent":
        return
    new_title = await chat_history.generate_title(session)
    if new_title:
        chat_history.save_session(session)


# ---------- chat header action callbacks (KKallas/Imp#45) ----------


@cl.action_callback("chat_new")
async def on_chat_new(action: cl.Action) -> None:
    """Button-triggered version of the `/new` command. Identical effect."""
    await _handle_new_chat_command()


@cl.action_callback("chat_rename")
async def on_chat_rename(action: cl.Action) -> None:
    """Prompt the admin for a new title. Manual renames flip
    `title_source` to "user" so the next agent-titling pass skips
    this session."""
    session = _current_session()
    if session is None:
        await cl.Message(
            author="Foreman",
            content="_(No active chat session to rename.)_",
        ).send()
        return
    resp = await cl.AskUserMessage(
        author="Foreman",
        content=(
            f"Current title: **{session.title}**\n\n"
            f"Type a new title (3-6 words works best). Your title locks "
            f"this chat against future agent re-titling."
        ),
        timeout=120,
    ).send()
    if not resp:
        return
    new = (resp.get("output") if isinstance(resp, dict) else None) or ""
    new = new.strip()
    if not new:
        return
    session.rename(new, by="user")
    chat_history.save_session(session)
    await _render_chat_header(session)


@cl.on_message
async def on_message(msg: cl.Message) -> None:
    # Every exit path ends with a budget-bar refresh so the bar is always
    # the last message in the transcript (pinned right above the input).
    try:
        await _on_message_body(msg)
    finally:
        await refresh_budget_bar()


async def _on_message_body(msg: cl.Message) -> None:
    if not is_setup_complete():
        await run_setup_agent()
        return

    # ---- Chat freeze: scenario session is open (KKallas/Imp#16) ----
    # While a scenario session is awaiting commit, free-form chat is
    # refused. The admin must pick a scenario via the action buttons or
    # click "close" to abandon. This forces the decision and prevents
    # accidental drift mid-deliberation.
    active_scenario = cl.user_session.get("active_scenario_session_id")
    if active_scenario:
        await cl.Message(
            author="Foreman",
            content=(
                f"A scenario session is open (`{active_scenario}`). The chat "
                f"is frozen until you pick a scenario or close the session. "
                f"Use the action buttons below the grid to proceed."
            ),
        ).send()
        return

    # ---- Checkpoint A: screen every inbound message ----
    # The guard sees the raw text and decides whether it's safe to pass
    # to the worker. On reject, the worker never sees this turn.
    from server import guard

    approved, reason = await guard.check_user_input(msg.content)
    if not approved:
        await cl.Message(
            author="Guard",
            content=f"**Blocked by checkpoint A.**\n\n{reason}",
        ).send()
        return

    text = msg.content.lower().strip()

    # KKallas/Imp#45: `/new` / "new chat" rotates the session. The
    # current chat's file stays on disk; a fresh session takes over.
    # This runs before guard checkpoint A because it's a local-only
    # control command, not a message the worker needs to see.
    if text in ("/new", "new chat", "/new chat"):
        await _handle_new_chat_command()
        return

    # Demo hook for P2.6: `run: <cmd>` runs the command through the real
    # server/intercept.py pipeline. See tests/test_intercept.py for the
    # underlying contract. This is how you'd exercise the interception
    # spine end-to-end before the real Foreman worker lands (KKallas/Imp#11).
    content_lower = msg.content.lower()
    if content_lower.startswith("run:") or content_lower.startswith("run "):
        await run_demo_command(msg.content)
        return

    # `log <action_id>` or `logs` (list recent)
    if (
        content_lower.startswith("log ")
        or content_lower.startswith("logs")
        or content_lower == "log"
        or content_lower.startswith("show log")
    ):
        await show_log_command(msg.content)
        return

    # `reset setup` is a config-mutation with no argv equivalent, so it
    # stays as a local shortcut instead of going through the dispatcher.
    if "reset" in text and "setup" in text:
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
        return

    # ---- Hand off to Foreman ----
    # P4.11 swaps the P2.9 JSON-verdict dispatcher for the real Foreman
    # agent: native claude-agent-sdk tool use, multi-turn via
    # ClaudeSDKClient, system prompt from v0.1.md §The Agent's Role.
    # Every shell invocation still routes through intercept.execute_command
    # (via Foreman's MCP tools) so the guard + budgets stay in force.
    # Explicit-mode shortcuts (run: and log) above are kept for their
    # richer UX (verdict table, log sidebar).
    from server import foreman_agent

    # KKallas/Imp#45: feed prior turns from the current session into
    # dispatch so Foreman can reference earlier messages naturally
    # ("now do the same for issue 43" after "moderate issue 42").
    session = _current_session()
    history_turns = list(session.turns) if session is not None else []

    # Log the user's turn BEFORE the call so a mid-dispatch crash still
    # leaves a record of what was asked.
    if session is not None:
        session.append_turn("user", msg.content)
        session.truncate()
        chat_history.save_session(session)

    reply = await foreman_agent.dispatch(
        msg.content,
        say=_foreman_say,
        ask=_foreman_ask,
        thinking=_foreman_thinking,
        chart=_foreman_chart,
        history=history_turns,
        turn_ui=_ForemanTurnUI(),
    )

    # Record the assistant turn + persist + agent-title after the first
    # real reply. All post-turn work is best-effort: a failure here
    # should never undo the reply the admin already saw.
    if session is not None:
        try:
            session.append_turn("assistant", reply)
            session.truncate()
            chat_history.save_session(session)
            await _maybe_retitle_session(session)
            await _render_chat_header(session)
        except Exception as exc:  # noqa: BLE001 — don't crash the turn on post-save
            import sys as _sys

            print(
                f"[chat-history] post-turn save failed: "
                f"{type(exc).__name__}: {exc}",
                file=_sys.stderr,
            )


async def _foreman_say(text: str) -> None:
    """Post a `Foreman`-authored reply to the admin.

    Includes a **mermaid watchdog** (KKallas/Imp#52): scans the outbound
    text for fenced mermaid blocks.  Each block is screenshotted to PNG
    via the render server and shown inline as an image with a link to
    the interactive viewer underneath.
    """
    cleaned, elements = await _apply_mermaid_watchdog(text)

    await cl.Message(
        author="Foreman",
        content=cleaned.strip(),
        elements=elements if elements else None,
    ).send()


async def _foreman_ask(question: str) -> str | None:
    """Ask the admin a single clarifying question. Returns the answer,
    or None if the admin timed out / dismissed.

    Chainlit's `cl.AskUserMessage.send()` returns either None or a dict
    whose `output` key holds the reply text. Normalise both to the
    dispatcher's `Optional[str]` contract.
    """
    resp = await cl.AskUserMessage(
        author="Foreman",
        content=question,
        timeout=120,
    ).send()
    if not resp:
        return None
    answer = (resp.get("output") if isinstance(resp, dict) else None) or ""
    answer = answer.strip()
    return answer or None


@asynccontextmanager
async def _foreman_thinking(label: str):
    """Bracket a slow LLM call with a visible `cl.Step` spinner.

    Foreman's `dispatch` enters this around the SDK conversation so the
    admin sees a "thinking…" step instead of an awkward silent pause.
    The step auto-closes (spinner clears) as soon as the dispatch
    returns, regardless of success or failure.
    """
    async with cl.Step(name=label, type="run") as step:
        yield step


# ---------- structured turn UI (KKallas/Imp#55) ----------


_MERMAID_IMG_DIR = Path(__file__).resolve().parent / "public" / "images"


async def _apply_mermaid_watchdog(text: str) -> tuple[str, list]:
    """Screenshot mermaid blocks to PNG and return ``(cleaned_text, elements)``.

    Each mermaid code block is:
      1. Rendered to HTML via the mermaid template.
      2. Screenshotted to PNG (5 s animation delay).
      3. Replaced in the text with a viewer link.
      4. Returned as a ``cl.Image`` element for inline display.

    Falls back to the old Plotly conversion for gantt blocks when the
    screenshot engine is unavailable.
    """
    from hashlib import sha256
    from urllib.parse import quote

    from pipeline.mermaid_to_plotly import extract_mermaid_blocks

    blocks = extract_mermaid_blocks(text)
    elements: list = []
    cleaned = text

    if not blocks:
        return cleaned, elements

    from server.render_route import _render_template
    from server.screenshot import available as _ss_available

    for block in blocks:
        content = block["content"]
        viewer_url = ""
        if _RENDER_BASE_URL:
            viewer_url = (
                f"{_RENDER_BASE_URL}/render/mermaid"
                f"?diagram={quote(content)}&mode=viewer"
            )

        # Try screenshot → PNG inline image
        if _ss_available():
            try:
                html = _render_template("mermaid", {"diagram": content})
                from server.screenshot import screenshot

                png = await screenshot(html)
                _MERMAID_IMG_DIR.mkdir(parents=True, exist_ok=True)
                slug = sha256(content.encode()).hexdigest()[:12]
                img_name = f"mermaid_{slug}.png"
                img_path = _MERMAID_IMG_DIR / img_name
                img_path.write_bytes(png)

                elements.append(
                    cl.Image(
                        name=img_name,
                        path=str(img_path),
                        display="inline",
                        size="large",
                    )
                )
                link = (
                    f"[Open interactive viewer]({viewer_url})"
                    if viewer_url
                    else ""
                )
                cleaned = cleaned.replace(block["raw"], link)
                continue
            except Exception as exc:  # noqa: BLE001
                import sys

                print(
                    f"[mermaid-watchdog] screenshot failed: "
                    f"{type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )

        # Fallback: gantt → Plotly, others → viewer link only
        first_word = content.lstrip().split()[0].lower() if content.strip() else ""
        if first_word == "gantt":
            try:
                from pipeline.mermaid_to_plotly import mermaid_gantt_to_plotly

                figure = mermaid_gantt_to_plotly(content)
                elements.append(
                    cl.Plotly(name="auto-gantt", figure=figure, display="inline")
                )
                cleaned = cleaned.replace(block["raw"], "")
            except Exception as exc:  # noqa: BLE001
                cleaned = cleaned.replace(
                    block["raw"],
                    block["raw"]
                    + f"\n\n_(couldn't render this gantt: {exc})_",
                )
        elif viewer_url:
            cleaned = cleaned.replace(
                block["raw"],
                f"[Open interactive viewer]({viewer_url})",
            )

    return cleaned, elements


class _ForemanTurnUI:
    """Chainlit implementation of the structured turn UI (KKallas/Imp#55).

    Everything renders inside **one** ``cl.Message`` that is updated in
    place — no separate ``cl.Step`` objects, so Chainlit's inter-element
    ordering quirks are eliminated.  The visual layout (top → bottom):

    1. **Plan checklist** — ⏳/✅/❌ per tool with timing
    2. **Thinking**      — blockquote with model's chain-of-thought
    3. **Answer**        — streamed prose (appended via ``stream_token``)

    Structure sections (1–2) are rebuilt via ``msg.update()`` whenever
    state changes.  The answer (3) is appended with ``msg.stream_token``
    for efficient incremental rendering.  Pure markdown — no HTML tags.
    """

    _LOGS_DIR = Path(__file__).resolve().parent / "public" / "logs"

    def __init__(self) -> None:
        self._msg: cl.Message | None = None
        self._plan_items: list | None = None
        self._thinking_chunks: list[str] = []
        self._answer_chunks: list[str] = []
        self._answer_started: bool = False
        self._tool_logs: dict[int, str] = {}  # index → "/public/logs/..." URL
        # Unique prefix so concurrent turns don't collide.
        import uuid
        self._log_prefix = uuid.uuid4().hex[:8]

    # -- helpers ------------------------------------------------------

    @staticmethod
    def _status_icon(status: str) -> str:
        return {
            "pending": "\u23f3",
            "running": "\U0001f504",
            "ok": "\u2705",
            "error": "\u274c",
        }.get(status, "\u23f3")

    def _render_structure(self) -> str:
        """Render plan + thinking — everything above the answer."""
        from server.turn_ui import format_tool_sig as _format_tool_sig

        parts: list[str] = []

        # Plan checklist — each tool on its own line with a log link
        if self._plan_items:
            lines = ["**Foreman's plan:**\n"]
            for i, item in enumerate(self._plan_items):
                icon = self._status_icon(item.status)
                sig = _format_tool_sig(item.name, item.args)
                timing = (
                    f" \u00b7 {item.duration_s:.1f}s"
                    if item.status in ("ok", "error")
                    else ""
                )
                log_link = ""
                if i in self._tool_logs:
                    log_link = f"  \u2014 [log]({self._tool_logs[i]})"
                lines.append(f"{icon} {sig}{timing}{log_link}")
            parts.append("\n".join(lines))

        # Thinking (blockquote)
        if self._thinking_chunks:
            thinking_text = "\n\n".join(self._thinking_chunks)
            quoted = "\n".join(
                f"> {line}" if line.strip() else ">"
                for line in thinking_text.splitlines()
            )
            parts.append(f"> **Foreman's thinking**\n>\n{quoted}")

        return "\n\n".join(parts)

    async def _ensure_msg(self) -> cl.Message:
        if self._msg is None:
            self._msg = cl.Message(author="Foreman", content="")
            await self._msg.send()
        return self._msg

    async def _update_structure(self) -> None:
        """Rebuild the message from structure + accumulated answer text."""
        base = self._render_structure()
        msg = await self._ensure_msg()
        content = base
        if self._answer_chunks:
            content += "\n\n" + "".join(self._answer_chunks)
        msg.content = content
        await msg.update()

    # -- TurnUI interface ---------------------------------------------

    async def show_plan(self, items: list) -> None:
        self._plan_items = items
        await self._update_structure()

    async def append_plan(self, items: list) -> None:
        await self._update_structure()

    async def tool_started(self, index: int, item: object) -> None:
        await self._update_structure()

    @staticmethod
    def _format_tool_log(name: str, args: dict, output: str) -> str:
        """Build a human-readable log body for a tool call."""
        lines = [f"Tool: {name}", ""]

        # Input
        lines.append("── Input ──")
        lines.append(json.dumps(args, indent=2))
        lines.append("")

        # Output — try to pretty-print the intercept JSON
        lines.append("── Output ──")
        try:
            data = json.loads(output)
            if isinstance(data, dict):
                meta: list[str] = []
                if "exit_code" in data:
                    meta.append(f"rc={data['exit_code']}")
                if data.get("verdict"):
                    meta.append(data["verdict"])
                if data.get("classified_as"):
                    meta.append(data["classified_as"])
                if meta:
                    lines.append(" · ".join(meta))
                if data.get("verdict_reason"):
                    lines.append(f"({data['verdict_reason']})")
                payload = data.get("output", "")
                if payload:
                    lines.append("")
                    lines.append(payload.replace("\t", "  |  "))
                remaining = {
                    k: v for k, v in data.items()
                    if k not in ("exit_code", "output", "action_id",
                                 "verdict", "verdict_reason", "classified_as")
                }
                if remaining:
                    lines.append("")
                    lines.append(json.dumps(remaining, indent=2))
            else:
                lines.append(json.dumps(data, indent=2))
        except (json.JSONDecodeError, TypeError):
            lines.append(output)

        return "\n".join(lines)

    async def tool_finished(self, index: int, item: object) -> None:
        from server.turn_ui import PlanItem

        assert isinstance(item, PlanItem)

        # Write a log file for this tool call.
        try:
            self._LOGS_DIR.mkdir(parents=True, exist_ok=True)
            filename = f"{self._log_prefix}_{index}_{item.name}.txt"
            log_path = self._LOGS_DIR / filename
            log_body = self._format_tool_log(item.name, item.args, item.output)
            log_path.write_text(log_body, encoding="utf-8")
            self._tool_logs[index] = f"/public/logs/{filename}"
        except OSError as exc:
            print(
                f"[turn-ui] log write failed: {exc}",
                file=__import__("sys").stderr,
            )

        await self._update_structure()

    async def stream_token(self, token: str) -> None:
        self._answer_chunks.append(token)
        if not self._answer_started:
            self._answer_started = True
            # Refresh structure (includes thinking) right before answer
            base = self._render_structure()
            msg = await self._ensure_msg()
            msg.content = base
            await msg.update()
        await self._msg.stream_token(token)  # type: ignore[union-attr]

    async def stream_end(self, full_text: str) -> None:
        cleaned, elements = await _apply_mermaid_watchdog(full_text)
        self._answer_chunks = [cleaned.strip()] if cleaned.strip() else []

        base = self._render_structure()
        content = base
        if self._answer_chunks:
            content += "\n\n" + self._answer_chunks[0]

        msg = await self._ensure_msg()
        msg.content = content
        if elements:
            msg.elements = elements  # type: ignore[assignment]
        await msg.update()

    async def thinking_update(self, text: str) -> None:
        self._thinking_chunks.append(text)
        # Don't rebuild yet — thinking is included when the structure
        # is next refreshed (on stream start or stream_end).


# ---------- scenario session UI (KKallas/Imp#16) ----------


async def _foreman_chart(artifact: dict) -> None:
    """Render an artifact produced by a Foreman tool call.

    Registered artifact types:
      - scenario_session : scenario-comparison grid (Plotly subplot
        + metric table + commit/switch/close buttons).
      - chart_file : `pipeline/render_chart.py` output. Renders the
        Plotly figure inline when present (burndown) and always
        attaches the HTML file as a download chip so the full
        interactive page is one click away.

    Unknown types log a warning and are skipped — a single bad tool
    output shouldn't kill the turn.
    """
    artifact_type = artifact.get("type")
    if artifact_type == "scenario_session":
        await _render_scenario_grid(artifact)
    elif artifact_type == "chart_file":
        await _render_chart_file(artifact)
    else:
        await cl.Message(
            author="Foreman",
            content=f"_(Unknown artifact type `{artifact_type}` — skipped.)_",
        ).send()


_PUBLIC_CHARTS_DIR = Path(__file__).resolve().parent / "public" / "charts"


def _publish_chart_html(html_path: Path, template: str) -> str | None:
    """Copy the rendered HTML into `public/charts/` so Chainlit's
    built-in `GET /public/<filename>` route serves it, and return the
    URL path. Markdown links in Chainlit get `target="_blank"` set on
    the rendered `<a>` automatically — so a plain `[label](url)` opens
    the full interactive page in a new browser tab rather than the
    chat-embedded download chip.

    Returns None when the copy fails — callers fall back to no link.
    """
    import shutil
    import sys

    try:
        _PUBLIC_CHARTS_DIR.mkdir(parents=True, exist_ok=True)
        dest = _PUBLIC_CHARTS_DIR / f"{template}.html"
        shutil.copyfile(html_path, dest)
    except OSError as exc:
        print(
            f"[main] _publish_chart_html({template!r}) failed: {exc}",
            file=sys.stderr,
        )
        return None
    return f"/public/charts/{template}.html"


async def _render_chart_file(artifact: dict) -> None:
    """Display a `pipeline/render_chart.py` output in the chat.

    Layout: inline Plotly figure (when the template has a native
    Plotly build — burndown does, the others don't yet) + a markdown
    link that opens the full self-contained HTML page in a new tab.
    The HTML is copied into `public/charts/` so Chainlit's `/public`
    static route can serve it; the frontend auto-targets markdown
    links with `target="_blank"`.
    """
    template = artifact.get("template") or "chart"
    html_path = artifact.get("path")
    plotly_figure = artifact.get("plotly_figure")

    import sys

    elements: list = []
    if plotly_figure:
        try:
            elements.append(
                cl.Plotly(
                    name=f"{template}-chart",
                    figure=plotly_figure,
                    display="inline",
                )
            )
        except Exception as exc:  # noqa: BLE001 — never break the turn
            print(
                f"[main] cl.Plotly failed for {template!r}: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )

    public_url: str | None = None
    if html_path and Path(html_path).exists():
        public_url = _publish_chart_html(Path(html_path), template)

    # Screenshot the chart HTML to PNG for inline display.
    if html_path and Path(html_path).exists():
        from server.screenshot import available as _ss_available

        if _ss_available():
            try:
                from server.screenshot import screenshot

                chart_html = Path(html_path).read_text()
                png = await screenshot(chart_html)
                _CHART_IMG_DIR = Path(__file__).resolve().parent / "public" / "images"
                _CHART_IMG_DIR.mkdir(parents=True, exist_ok=True)
                img_name = f"{template}_chart.png"
                img_path = _CHART_IMG_DIR / img_name
                img_path.write_bytes(png)
                elements.append(
                    cl.Image(
                        name=img_name,
                        path=str(img_path),
                        display="inline",
                        size="large",
                    )
                )
            except Exception as exc:  # noqa: BLE001
                print(
                    f"[main] chart screenshot failed for {template!r}: "
                    f"{type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )

    if not elements and not public_url:
        await cl.Message(
            author="Foreman",
            content=(
                f"_(Rendered `{template}` chart but the output file is missing "
                f"and no inline figure is available — nothing to show.)_"
            ),
        ).send()
        return

    parts: list[str] = [f"**{template.capitalize()} chart**"]
    if public_url:
        parts.append(f"[Open full page in a new tab]({public_url})")
    content = "\n\n".join(parts)

    await cl.Message(
        author="Foreman",
        content=content,
        elements=elements,
    ).send()


async def _render_scenario_grid(artifact: dict) -> None:
    """Render a scenario session as a one-message grid: Plotly subplot
    at the top, Markdown metric table below, action buttons for commit
    / switch / close. Also freezes the chat by setting the session id
    in `cl.user_session`."""
    session_id = artifact["session_id"]
    scenarios = artifact.get("scenarios") or []
    descriptions = artifact.get("descriptions") or []
    committed_choice = artifact.get("committed_choice")
    baseline_empty = artifact.get("baseline_empty")

    # Plotly subplot grid — one column per scenario.
    plotly_element: cl.Plotly | None = None
    if any(s.get("charts") for s in scenarios):
        plotly_element = _build_scenario_subplot(scenarios, descriptions)

    # Markdown metric table — scenarios as columns.
    table_md = _build_scenario_metric_table(scenarios, descriptions, committed_choice)

    # Action buttons — commit each scenario + switch (if already committed)
    # + close-without-commit escape hatch.
    actions: list[cl.Action] = []
    for idx, sc in enumerate(scenarios):
        label_prefix = "Switch to" if committed_choice is not None else "Commit"
        actions.append(
            cl.Action(
                name="scenario_commit",
                payload={"session_id": session_id, "choice_index": idx},
                label=f"{label_prefix} s{idx + 1}",
                tooltip=f"{label_prefix} scenario {idx + 1}: {sc.get('name', '?')}",
            )
        )
    actions.append(
        cl.Action(
            name="scenario_close",
            payload={"session_id": session_id},
            label="Close without committing",
            tooltip="Close the session. Files stay on disk; re-openable later.",
        )
    )

    warning_prefix = ""
    if baseline_empty:
        warning_prefix = (
            "> **Warning:** no enriched baseline data — run `sync issues` then "
            "`run heuristics` first, then re-open this session.\n\n"
        )

    header = f"### Scenario session `{session_id}`\n\n"
    if committed_choice is not None:
        header += (
            f"_Current commit: **s{committed_choice + 1}** "
            f"({scenarios[committed_choice].get('name', '?')})._\n\n"
        )

    content = warning_prefix + header + table_md

    elements = [plotly_element] if plotly_element is not None else []

    await cl.Message(
        author="Foreman",
        content=content,
        elements=elements,
        actions=actions,
    ).send()

    # Freeze the chat until a button is clicked.
    cl.user_session.set("active_scenario_session_id", session_id)


def _build_scenario_subplot(
    scenarios: list[dict], descriptions: list[str]
) -> cl.Plotly | None:
    """Combine the first chart of each scenario into a single Plotly
    figure with **vertical** subplots (one row per scenario) — full
    width each so labels and bars stay readable on a phone. Returns
    None if no scenario produced a chart."""
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except ImportError:
        return None

    first_charts = [s["charts"][0] if s.get("charts") else None for s in scenarios]
    if not any(first_charts):
        return None

    titles = [
        (descriptions[i] if i < len(descriptions) else s.get("name", f"s{i+1}"))[:60]
        for i, s in enumerate(scenarios)
    ]
    n = len(scenarios)
    fig = make_subplots(
        rows=n,
        cols=1,
        subplot_titles=titles,
        shared_xaxes=False,
        vertical_spacing=0.08,
    )

    # Per-chart height: ~24px per bar + 80px title/axis overhead, with a
    # minimum so an empty scenario doesn't collapse the row.
    per_chart_rows: list[int] = []

    for idx, chart in enumerate(first_charts):
        if chart is None:
            per_chart_rows.append(0)
            continue
        row = idx + 1
        bar_count = 0
        for trace in chart.get("data", []):
            fig.add_trace(
                go.Bar(**trace) if trace.get("type") == "bar" else trace,
                row=row,
                col=1,
            )
            if isinstance(trace.get("y"), list):
                bar_count = max(bar_count, len(trace["y"]))
        per_chart_rows.append(bar_count)

        # Each subplot keeps its own date axis + reversed y so the first
        # issue appears at the top of that scenario's panel.
        src_layout = chart.get("layout", {})
        if isinstance(src_layout.get("xaxis"), dict):
            xaxis_type = src_layout["xaxis"].get("type")
            if xaxis_type:
                fig.update_xaxes(type=xaxis_type, row=row, col=1)
        fig.update_yaxes(autorange="reversed", automargin=True, row=row, col=1)

    total_height = sum(max(180, 24 * rows + 80) for rows in per_chart_rows)
    fig.update_layout(
        height=total_height + 40 * n,  # slack for subplot titles
        showlegend=False,
        margin=dict(l=20, r=20, t=40, b=30),
    )

    return cl.Plotly(name="scenario-grid", figure=fig, display="inline")


def _build_scenario_metric_table(
    scenarios: list[dict],
    descriptions: list[str],
    committed_choice: int | None,
) -> str:
    """Build a Markdown table with scenarios as columns and all
    emitted metrics / lists / texts as rows. Rows that a given scenario
    didn't produce get a `—` placeholder."""
    # Collect all unique row keys across all scenarios, preserving order.
    row_keys: list[str] = []
    seen: set[str] = set()
    for sc in scenarios:
        for name, _ in sc.get("metrics") or []:
            if name not in seen:
                row_keys.append(name)
                seen.add(name)
        for name, _ in sc.get("lists") or []:
            if name not in seen:
                row_keys.append(name)
                seen.add(name)
        for name, _ in sc.get("texts") or []:
            if name not in seen:
                row_keys.append(name)
                seen.add(name)

    if not row_keys:
        return "_(No metrics emitted by any scenario.)_\n"

    # Header row: scenario labels.
    header_cells = ["**Metric**"]
    for idx, sc in enumerate(scenarios):
        label = descriptions[idx] if idx < len(descriptions) else sc.get("name", f"s{idx+1}")
        marker = " ✓" if committed_choice == idx else ""
        header_cells.append(f"**s{idx + 1}{marker}: {label[:40]}**")
    header = "| " + " | ".join(header_cells) + " |"
    separator = "|" + "|".join(["---"] * len(header_cells)) + "|"

    rows: list[str] = [header, separator]

    for key in row_keys:
        row = [f"`{key}`"]
        for sc in scenarios:
            value = _find_output_value(sc, key)
            row.append(value or "—")
        rows.append("| " + " | ".join(row) + " |")

    return "\n".join(rows) + "\n"


def _find_output_value(scenario: dict, key: str) -> str:
    """Look up a row value by name across metrics / lists / texts.
    First match wins; list values are bullet-separated."""
    for name, value in scenario.get("metrics") or []:
        if name == key:
            return str(value)
    for name, items in scenario.get("lists") or []:
        if name == key:
            if not items:
                return "_(none)_"
            return ", ".join(str(i) for i in items)
    for name, content in scenario.get("texts") or []:
        if name == key:
            return str(content)
    return ""


@cl.action_callback("scenario_commit")
async def on_scenario_commit(action: cl.Action) -> None:
    """Action callback: commit a scenario choice, unfreeze the chat,
    and render a fresh gantt HTML for the committed scenario so the
    admin sees the final "clean" view right where they confirmed the
    choice — same inline Plotly + open-in-new-tab link the other
    template renders use.
    """
    payload = action.payload or {}
    session_id = payload.get("session_id")
    choice_index = payload.get("choice_index")
    if not isinstance(session_id, str) or not isinstance(choice_index, int):
        await cl.Message(
            author="Foreman",
            content=f"_(bad scenario_commit payload: {payload!r})_",
        ).send()
        return

    from server import foreman_agent

    result = await foreman_agent.do_commit_scenario(session_id, choice_index)
    # Unfreeze regardless of success/failure — admin can retry if it failed.
    cl.user_session.set("active_scenario_session_id", None)

    if "error" in result:
        await cl.Message(
            author="Foreman",
            content=f"**Commit failed:** {result['error']}",
        ).send()
        return

    committed = result.get("committed") or {}
    scenario_name = committed.get("choice_name", f"s{choice_index + 1}")
    await cl.Message(
        author="Foreman",
        content=(
            f"**Committed.** Active scenario: `{scenario_name}`. "
            f"Rendering the gantt for this scenario below — baseline data "
            f"on disk is unchanged; this is the lensed view."
        ),
    ).send()

    # Render the committed scenario's gantt as a chart_file artifact.
    # `render_chart.py` picks up the active_scenario.json pointer on
    # load and applies the lens, so the output reflects the commit
    # we just made.
    await _render_committed_scenario_gantt(scenario_name)


async def _render_committed_scenario_gantt(scenario_name: str) -> None:
    """Trigger `run_render_chart --template gantt` from outside the
    normal `dispatch()` flow and render any chart_file artifacts it
    pushes. Errors are logged but never break the commit confirmation
    — a failed render shouldn't undo a successful commit."""
    import sys

    from server import foreman_agent

    # Any artifacts already queued belong to a previous turn; clear so
    # we don't re-render stale ones here.
    foreman_agent._pending_artifacts.clear()

    try:
        await foreman_agent.do_run_render_chart(
            template="gantt",
            user_intent=f"render committed scenario '{scenario_name}'",
        )
    except Exception as exc:  # noqa: BLE001
        print(
            f"[main] render after commit failed: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        await cl.Message(
            author="Foreman",
            content=(
                f"_(Chart render after commit failed — "
                f"`{type(exc).__name__}`. Ask me to `show gantt` to retry.)_"
            ),
        ).send()
        return

    # Drain the queue manually since we're outside dispatch().
    for artifact in list(foreman_agent._pending_artifacts):
        try:
            await _foreman_chart(artifact)
        except Exception as exc:  # noqa: BLE001
            print(
                f"[main] _foreman_chart failed on commit-render: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
    foreman_agent._pending_artifacts.clear()


@cl.action_callback("scenario_close")
async def on_scenario_close(action: cl.Action) -> None:
    """Action callback: close a scenario session without committing."""
    payload = action.payload or {}
    session_id = payload.get("session_id")
    if not isinstance(session_id, str):
        await cl.Message(
            author="Foreman",
            content=f"_(bad scenario_close payload: {payload!r})_",
        ).send()
        return

    from server import foreman_agent

    await foreman_agent.do_close_scenario(session_id)
    cl.user_session.set("active_scenario_session_id", None)
    await cl.Message(
        author="Foreman",
        content=(
            f"Session `{session_id}` closed without commit. "
            f"Files stay on disk — reopen with 'open scenario session "
            f"{session_id}' later if you want."
        ),
    ).send()


# ---------- real intercept demo (P2.6) ----------


async def run_demo_command(user_content: str) -> None:
    """Run `run: <cmd>` or `run <cmd>` through server/intercept.py.

    This is the P2.6 demo: the interception pipeline is real — the
    subprocess is spawned for real, stdout/stderr stream live into the
    `cl.Step`, and the final verdict / budget update is applied. The
    guard in `server/guard.py` screens writes via checkpoint B.

    In this demo the user types the command directly, so `user_intent`
    always matches the command — the guard will always approve. Real
    guard rejections happen when the Foreman worker (KKallas/Imp#11)
    proposes a command that diverges from the user's stated intent (e.g.
    a malicious issue body tricked it into closing unrelated issues).
    """
    import shlex

    from server import intercept

    # Strip the "run:" or "run " prefix, preserving the rest verbatim
    stripped = user_content.strip()
    if stripped.lower().startswith("run:"):
        cmd_str = stripped[4:].strip()
    else:
        cmd_str = stripped[4:].strip()
    if not cmd_str:
        await cl.Message(
            author="Foreman",
            content="Usage: `run: <command>` — e.g. `run: echo hello` or `run: date`.",
        ).send()
        return

    try:
        argv = shlex.split(cmd_str)
    except ValueError as e:
        await cl.Message(
            author="Foreman",
            content=f"Couldn't parse the command (`{e}`). Try again with simpler quoting.",
        ).send()
        return

    async with cl.Step(name=f"intercept: {' '.join(argv)}", type="tool") as step:
        step.input = cmd_str
        rc, output, action = await intercept.execute_command(
            argv,
            user_intent=user_content,
            rationale="Admin demo of server/intercept.py via chat",
            kind="demo",
            step=step,
        )

    # Refresh happens at the end of on_message via the try/finally — no
    # extra call needed here.

    # Collapsed-by-default step with just the verdict table. No elements
    # attached — we use a separate action button below the step so the
    # user can explicitly open the log sidebar, instead of Chainlit
    # auto-pushing side elements on step completion.
    summary = (
        f"intercept.py result · {action.action_id} · "
        f"{action.verdict} · rc={rc}"
    )
    async with cl.Step(
        name=summary,
        type="tool",
        default_open=False,
    ) as verdict_step:
        verdict_step.input = cmd_str
        verdict_step.output = (
            f"| Field | Value |\n"
            f"|---|---|\n"
            f"| action_id | `{action.action_id}` |\n"
            f"| classified_as | `{action.classified_as}` |\n"
            f"| verdict | `{action.verdict}` |\n"
            f"| verdict_reason | {action.verdict_reason or '—'} |\n"
            f"| return code | `{rc}` |"
        )

    # Below the step: a small message with an action button (opens the
    # log sidebar on click — closing and re-clicking works because the
    # callback explicitly calls ElementSidebar.set_elements every time)
    # plus an inline download chip. Sidebar stays closed until the user
    # explicitly clicks the "Open log" button.
    log_path = intercept.OUTPUT_DIR / f"{action.action_id}.log"
    if log_path.exists():
        await cl.Message(
            author="Foreman",
            content=f"📄 `{action.action_id}.log`:",
            actions=[
                cl.Action(
                    name="open_log_sidebar",
                    payload={"action_id": action.action_id},
                    label="📂 Open log",
                    tooltip="View the log contents in the side panel",
                ),
            ],
            elements=[
                cl.File(
                    name=f"{action.action_id}.log",
                    path=str(log_path),
                    display="inline",
                    mime="text/plain",
                ),
            ],
        ).send()


async def _view_log_by_id(action_id: str) -> None:
    """Post a clickable link to `.imp/output/<action_id>.log`.

    Shared by the `log <id>` chat command and the `view_log` action
    callback. Accepts either `act_xxxxxxxx` or bare `xxxxxxxx`.

    Posts a message with two elements: a `cl.Text(display="side")`
    that shows the contents when clicked, and a `cl.File(display="inline")`
    as a download chip.
    """
    from server import intercept

    if not action_id.startswith("act_"):
        action_id = "act_" + action_id
    log_path = intercept.OUTPUT_DIR / f"{action_id}.log"
    if not log_path.exists():
        await cl.Message(
            author="Foreman",
            content=(
                f"No log file at `{log_path}`. Check the action_id, or type "
                f"`logs` to list recent ones."
            ),
        ).send()
        return

    try:
        content = log_path.read_text()
    except OSError as e:
        await cl.Message(
            author="Foreman",
            content=f"Couldn't read `{log_path}`: {e}",
        ).send()
        return

    size = log_path.stat().st_size
    await cl.Message(
        author="Foreman",
        content=f"Log `{action_id}.log` ({size} bytes):",
        elements=[
            cl.Text(
                name=f"{action_id}.log",
                content=content or "(empty)",
                display="side",
            ),
            cl.File(
                name=f"{action_id}.log",
                path=str(log_path),
                display="inline",
                mime="text/plain",
            ),
        ],
    ).send()


@cl.action_callback("view_log")
async def on_view_log_action(action: cl.Action) -> None:
    """Handler for the clickable log buttons attached to the `logs` listing."""
    action_id = (action.payload or {}).get("action_id")
    if not action_id:
        return
    await _view_log_by_id(action_id)


@cl.action_callback("open_log_sidebar")
async def on_open_log_sidebar(action: cl.Action) -> None:
    """Open the side panel with the log contents.

    Explicitly calls cl.ElementSidebar.set_elements every time — so if
    the user closes the panel and clicks the button again, a fresh
    sidebar opens with the same content. This is the only reliable way
    I've found to get a "reopen the log" button in Chainlit 2.11.

    Payload: {"action_id": "act_xxxxxxxx"}
    """
    from server import intercept

    action_id = (action.payload or {}).get("action_id")
    if not action_id:
        return
    log_path = intercept.OUTPUT_DIR / f"{action_id}.log"
    if not log_path.exists():
        return
    try:
        content = log_path.read_text()
    except OSError:
        return
    try:
        await cl.ElementSidebar.set_title(f"{action_id}.log")
    except Exception:
        pass
    await cl.ElementSidebar.set_elements(
        [
            cl.Text(
                name=f"{action_id}.log",
                content=content or "(empty)",
                display="side",
            ),
        ]
    )


async def show_log_command(user_content: str) -> None:
    """Chat command: `log <action_id>` or `logs` (list with clickable chips).

    Reads `.imp/output/<action_id>.log` directly from disk (not from
    intercept.action_log, which is in-memory and may have rotated). Works
    for any action whose log file still exists, even from a previous
    session.

    With no argument, lists the 10 most recent log files as clickable
    `cl.Action` buttons — click any button to view that log inline.
    """
    from server import intercept

    arg = user_content.strip()
    # Drop the leading "log" / "logs" / "show log" / "show logs"
    for prefix in ("show logs", "show log", "logs", "log"):
        if arg.lower().startswith(prefix):
            arg = arg[len(prefix) :].strip()
            break

    if not arg:
        # List recent log files with clickable action buttons
        if not intercept.OUTPUT_DIR.exists():
            await cl.Message(
                author="Foreman",
                content="No logs yet. Run something with `run: <cmd>` first.",
            ).send()
            return
        files = sorted(
            intercept.OUTPUT_DIR.glob("act_*.log"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:10]
        if not files:
            await cl.Message(
                author="Foreman",
                content="No logs yet. Run something with `run: <cmd>` first.",
            ).send()
            return
        actions = [
            cl.Action(
                name="view_log",
                payload={"action_id": f.stem},
                label=f.stem,
                tooltip=f"{f.stat().st_size} bytes",
            )
            for f in files
        ]
        await cl.Message(
            author="Foreman",
            content=(
                f"**Recent logs ({len(files)})** — click any button to view "
                f"inline."
            ),
            actions=actions,
        ).send()
        return

    # View a specific log by id
    action_id = arg.split()[0]  # take first token in case of trailing text
    await _view_log_by_id(action_id)


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
        step.output = "Built plotly timeline figure"

    import pandas as pd
    import plotly.express as px

    tasks = [
        {"Task": "Phase 1 — chat shell + auth",        "Start": "2026-04-15", "End": "2026-04-22", "Phase": "P1"},
        {"Task": "Phase 2 — security spine",           "Start": "2026-04-22", "End": "2026-04-27", "Phase": "P2"},
        {"Task": "Phase 3 — Setup Agent",              "Start": "2026-04-27", "End": "2026-05-01", "Phase": "P3"},
        {"Task": "Phase 4 — Foreman + visibility",     "Start": "2026-05-01", "End": "2026-05-13", "Phase": "P4"},
        {"Task": "Phase 5 — wire 99-tools",            "Start": "2026-05-13", "End": "2026-05-18", "Phase": "P5"},
        {"Task": "Phase 6 — autonomous loop",          "Start": "2026-05-18", "End": "2026-05-22", "Phase": "P6"},
        {"Task": "Phase 7 — polish",                   "Start": "2026-05-22", "End": "2026-05-25", "Phase": "P7"},
    ]
    df = pd.DataFrame(tasks)
    fig = px.timeline(
        df,
        x_start="Start",
        x_end="End",
        y="Task",
        color="Phase",
        title="Imp v0.1 Build Phases (stub)",
    )
    fig.update_yaxes(autorange="reversed")  # Phase 1 at top
    fig.update_layout(
        showlegend=False,
        margin=dict(l=10, r=10, t=50, b=10),
    )

    await cl.Message(
        author="Foreman",
        content=(
            "Here's the current build timeline. Plotly's toolbar (top-right of "
            "the chart) has pan, zoom, box-select, reset-view, and **Download "
            "PNG**. Real data lands with KKallas/Imp#14."
        ),
        elements=[cl.Plotly(name="build_timeline", figure=fig, display="inline")],
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
    """Real-data budget status via `server.budgets`. Setters live in the
    Budgets panel (gear icon) — Foreman doesn't get tools to change them.
    """
    b = budgets.get_budgets()
    await cl.Message(
        author="Foreman",
        content=(
            "**Budget status**\n\n"
            "| Counter | Used | Limit | Remaining |\n"
            "|---|---:|---:|---:|\n"
            f"| Tokens | {b.tokens_used:,} | {b.tokens_limit:,} | {b.remaining('tokens'):,} |\n"
            f"| Edits  | {b.edits_used} | {b.edits_limit} | {b.remaining('edits')} |\n"
            f"| Tasks  | {b.tasks_used} | {b.tasks_limit} | {b.remaining('tasks')} |\n\n"
            "_Open the **gear icon** (top-right) to change limits or reset counters._"
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
