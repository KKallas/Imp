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
from chainlit.input_widget import NumberInput, Select, Switch

from server import budgets

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

    # ---- Hand off to the real dispatcher ----
    # P2.9 replaces the keyword-matcher below this line with an LLM-driven
    # dispatcher that routes to intercept.execute_command, asks clarifying
    # questions, or answers directly. Explicit-mode shortcuts (run:, keyword
    # argv) are handled inside `dispatcher._parse_explicit` and also above
    # via `run_demo_command` / `show_log_command`, which keep their richer
    # UX (verdict table, log sidebar).
    from server import dispatcher

    await dispatcher.dispatch(
        msg.content,
        say=_foreman_say,
        ask=_foreman_ask,
    )


async def _foreman_say(text: str) -> None:
    """Post a `Foreman`-authored reply to the admin."""
    await cl.Message(author="Foreman", content=text).send()


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
