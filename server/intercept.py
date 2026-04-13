"""server/intercept.py — action interception + runtime control spine.

The single chokepoint for every action that touches GitHub or runs a
pipeline script. Owns:

  1. **Classification** — `classify_command(argv)` returns read / write /
     unknown based on the gh subcommand, pipeline script path, or a
     small whitelist of safe demo commands.

  2. **Proposed actions** — each call to `execute_command(argv, ...)`
     builds a `ProposedAction` record (id, command, kind, rationale,
     user_intent, classification, timestamp, eventual verdict).

  3. **Guard check** — for writes, calls `guard.check(action)` (the real
     Guard Agent in `server/guard.py`, KKallas/Imp#7). The guard is a
     separate Claude session with no tools; it only emits a structured
     `{verdict, reason}` judgment. Reads skip the guard entirely. On
     backend errors the guard fails closed — writes are rejected, not
     silently approved.

  4. **Budget enforcement** — before executing, consults
     `server/budgets.py`. If `edits` or `tasks` is exhausted, or if the
     remaining token budget is below the per-invocation floor for a
     pipeline run, the action is rejected without calling the guard.
     **In-flight subprocesses are never killed by a budget tick** — the
     budget exhausts into the running task, not through it.

  5. **Execution** — approved actions are spawned via
     `asyncio.create_subprocess_exec`, with merged stdout/stderr streamed
     line-by-line into:
       - the caller's `cl.Step.output` (with `await step.update()` per
         line, so the browser sees a live `tail -f`)
       - `.imp/output/<action_id>.log` (so power users can `tail -f`
         from a separate terminal)

  6. **Running-tasks map** — `running_tasks: dict[str, RunningTask]`
     tracks every active subprocess by `action_id`, with PID, process,
     start time, cl.Step reference, cl.Task entry, and log path. Added
     on spawn, removed on exit.

  7. **Cancellation** — `abort_task(action_id)` sends SIGTERM, waits 15
     seconds for a clean exit, then sends SIGKILL if the process still
     lives. The pipeline scripts are expected to honor SIGTERM per the
     "SIGTERM contract for pipeline scripts" in v0.1.md.

  8. **Manual-mode pause** — `accepting_new_actions` flag persisted in
     `.imp/config.json`. When false, new writes are rejected without
     calling the guard. Reads still work. In-flight subprocesses are
     unaffected.

This module is intentionally independent of the Claude Agent SDK and
Chainlit: it takes argv lists and optional opaque `step` / `task_entry`
objects, and returns tuples. The SDK pre-tool-call hook (future,
foreman_agent.py) and the gh-shim fallback both become thin adapters
that call `execute_command()`.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Optional

from . import budgets, guard

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / ".imp"
OUTPUT_DIR = STATE_DIR / "output"
CONFIG_FILE = STATE_DIR / "config.json"

LOG_SIZE_CAP_MB = 100
SIGTERM_GRACE_SECONDS = 15


# ---------- classification ----------

ClassifyResult = Literal["read", "write", "unknown"]

# gh subcommand verbs we know are mutating
GH_WRITE_VERBS = {
    "edit",
    "create",
    "delete",
    "close",
    "reopen",
    "add",
    "remove",
    "set",
    "lock",
    "unlock",
    "comment",
    "item-edit",
    "item-create",
    "item-delete",
    "item-add",
    "item-archive",
    "field-create",
    "field-delete",
}

# gh subcommand verbs we know are read-only
GH_READ_VERBS = {
    "view",
    "list",
    "status",
    "browse",
    "ls",
    "search",
    "item-list",
    "field-list",
    "diff",
    "checks",
}

PIPELINE_READ_SCRIPTS = {
    "pipeline/sync_issues.py",
    "pipeline/heuristics.py",
    "pipeline/render_chart.py",
    "pipeline/scenario.py",
}

PIPELINE_WRITE_SCRIPTS = {
    "99-tools/moderate_issues.py",
    "99-tools/solve_issues.py",
    "99-tools/fix_prs.py",
    "99-tools/run_all.sh",
    "pipeline/project_bootstrap.py",
}

# Small whitelist of harmless commands so you can exercise intercept.py
# end-to-end from chat without wiring up a real pipeline script. These
# are classified as "read" (no guard, no budget).
DEMO_SAFE_COMMANDS = {"echo", "ls", "pwd", "date", "hostname", "whoami", "uname", "cat", "sleep"}


def classify_command(argv: list[str]) -> ClassifyResult:
    """Return `read`, `write`, or `unknown` for a shell command.

    The classification rules are the contract by which intercept.py
    decides whether to bypass the guard (read), call the guard (write),
    or refuse outright (unknown — fail closed).
    """
    if not argv:
        return "unknown"

    cmd = argv[0]
    basename = cmd.rsplit("/", 1)[-1]

    # gh subcommands — `gh <noun> <verb> [args]`
    if basename == "gh" and len(argv) >= 3:
        verb = argv[2]
        if verb in GH_READ_VERBS:
            return "read"
        if verb in GH_WRITE_VERBS:
            return "write"
        return "unknown"

    # Plain `gh auth status` (two tokens, read-only) and similar
    if basename == "gh" and len(argv) == 2:
        if argv[1] in ("auth", "--version", "version"):
            return "read"
        return "unknown"

    # python / python3 <script>
    if basename in ("python", "python3") and len(argv) >= 2:
        script = argv[1]
        for s in PIPELINE_READ_SCRIPTS:
            if script.endswith(s):
                return "read"
        for s in PIPELINE_WRITE_SCRIPTS:
            if script.endswith(s):
                return "write"
        return "unknown"

    # 99-tools/run_all.sh directly
    if cmd.endswith("/run_all.sh") or basename == "run_all.sh":
        return "write"

    # Demo-safe commands
    if basename in DEMO_SAFE_COMMANDS:
        return "read"

    return "unknown"


def is_pipeline_script(argv: list[str]) -> bool:
    """True if argv invokes a script that counts toward the tasks budget."""
    if not argv:
        return False
    if argv[0].rsplit("/", 1)[-1] in ("python", "python3") and len(argv) >= 2:
        return any(argv[1].endswith(s) for s in PIPELINE_WRITE_SCRIPTS)
    if argv[0].endswith("/run_all.sh") or argv[0].rsplit("/", 1)[-1] == "run_all.sh":
        return True
    return False


# ---------- data structures ----------


@dataclass
class ProposedAction:
    action_id: str
    command: list[str]
    kind: str
    rationale: str
    user_intent: str
    classified_as: ClassifyResult
    proposed_at: datetime
    verdict: Optional[str] = None  # "approve" | "reject"
    verdict_reason: Optional[str] = None
    returncode: Optional[int] = None
    finished_at: Optional[datetime] = None


@dataclass
class RunningTask:
    action_id: str
    proc: asyncio.subprocess.Process
    pid: int
    command: list[str]
    started_at: datetime
    # Loosely typed to avoid importing chainlit here — the caller passes
    # cl.Step / cl.Task instances but we only call .update() and .status
    # on them so we don't need the real types.
    step: Optional[Any] = None
    task_entry: Optional[Any] = None
    log_file: Optional[Path] = None


# ---------- module state ----------

running_tasks: dict[str, RunningTask] = {}
action_log: list[ProposedAction] = []

_accepting_new_actions: bool = True


def accepting_new_actions() -> bool:
    return _accepting_new_actions


def set_accepting_new_actions(value: bool) -> None:
    """Flip the manual-mode pause flag and persist it to .imp/config.json."""
    global _accepting_new_actions
    _accepting_new_actions = bool(value)
    cfg: dict = {}
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text())
        except json.JSONDecodeError:
            cfg = {}
    cfg["accepting_new_actions"] = _accepting_new_actions
    STATE_DIR.mkdir(exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


def _load_pause_flag_from_config() -> None:
    global _accepting_new_actions
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text())
            _accepting_new_actions = cfg.get("accepting_new_actions", True)
        except json.JSONDecodeError:
            pass


_load_pause_flag_from_config()


# ---------- log files ----------


def _new_action_id() -> str:
    return "act_" + secrets.token_hex(4)


def _log_file_for(action_id: str) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    return OUTPUT_DIR / f"{action_id}.log"


def _rotate_logs_if_needed() -> None:
    """Delete oldest `.log` files when `.imp/output/` exceeds the cap."""
    if not OUTPUT_DIR.exists():
        return
    files = [p for p in OUTPUT_DIR.glob("*.log") if p.is_file()]
    total = sum(p.stat().st_size for p in files)
    cap = LOG_SIZE_CAP_MB * 1024 * 1024
    if total <= cap:
        return
    files.sort(key=lambda p: p.stat().st_mtime)
    target = cap * 0.8  # leave 20% headroom after a rotation
    for f in files:
        total -= f.stat().st_size
        try:
            f.unlink()
        except OSError:
            pass
        if total <= target:
            break


# ---------- main execution ----------


async def execute_command(
    argv: list[str],
    *,
    user_intent: str = "",
    rationale: str = "",
    kind: str = "run",
    step: Optional[Any] = None,
    task_entry: Optional[Any] = None,
) -> tuple[int, str, ProposedAction]:
    """Classify → guard → budget → execute a shell command.

    Returns `(returncode, combined_output, action_record)`.

    Reads skip the guard entirely. Writes go through `guard.check` (and
    through budget checks). Unknown commands are refused outright.

    If `step` is a chainlit `cl.Step`, stdout/stderr are streamed into
    `step.output` with `await step.update()` per line. If `task_entry`
    is a chainlit `cl.Task`, its status is flipped to DONE/FAILED on
    exit.

    The action record is always appended to `action_log` regardless of
    whether the execution succeeded, failed, or was rejected.
    """
    action = ProposedAction(
        action_id=_new_action_id(),
        command=list(argv),
        kind=kind,
        rationale=rationale,
        user_intent=user_intent,
        classified_as=classify_command(argv),
        proposed_at=datetime.now(),
    )
    action_log.append(action)

    # Unknown commands — fail closed, never execute
    if action.classified_as == "unknown":
        action.verdict = "reject"
        action.verdict_reason = "unknown command — refusing to execute"
        action.finished_at = datetime.now()
        return (1, action.verdict_reason, action)

    # Manual-mode pause check (reads still pass)
    if not _accepting_new_actions and action.classified_as == "write":
        action.verdict = "reject"
        action.verdict_reason = (
            "Imp is paused (not accepting new write actions). Resume to continue."
        )
        action.finished_at = datetime.now()
        return (1, action.verdict_reason, action)

    # Budget check — only for writes / pipeline runs
    if action.classified_as == "write":
        b = budgets.get_budgets()
        if b.exhausted("tokens"):
            action.verdict = "reject"
            action.verdict_reason = (
                f"token budget exhausted ({b.tokens_used}/{b.tokens_limit}). "
                f"Raise the cap or reset the counter to continue."
            )
            action.finished_at = datetime.now()
            return (1, action.verdict_reason, action)
        if b.exhausted("edits"):
            action.verdict = "reject"
            action.verdict_reason = (
                f"edits budget exhausted ({b.edits_used}/{b.edits_limit}). "
                f"Raise the cap or reset the counter to continue."
            )
            action.finished_at = datetime.now()
            return (1, action.verdict_reason, action)
        if is_pipeline_script(argv):
            if b.exhausted("tasks"):
                action.verdict = "reject"
                action.verdict_reason = (
                    f"tasks budget exhausted ({b.tasks_used}/{b.tasks_limit})."
                )
                action.finished_at = datetime.now()
                return (1, action.verdict_reason, action)
            remaining_tokens = b.remaining("tokens")
            if remaining_tokens < budgets.PER_INVOCATION_CAP_FLOOR:
                action.verdict = "reject"
                action.verdict_reason = (
                    f"not enough token budget to start a new pipeline task "
                    f"({remaining_tokens} tokens remaining < floor of "
                    f"{budgets.PER_INVOCATION_CAP_FLOOR})"
                )
                action.finished_at = datetime.now()
                return (1, action.verdict_reason, action)

    # Guard check (writes only)
    if action.classified_as == "write":
        approved, reason = await guard.check(action)
        action.verdict = "approve" if approved else "reject"
        action.verdict_reason = reason
        if not approved:
            action.finished_at = datetime.now()
            return (1, f"Guard rejected: {reason}", action)
    else:
        # read
        action.verdict = "approve"
        action.verdict_reason = "read command — no guard check"

    # Execute
    log_path = _log_file_for(action.action_id)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=ROOT,
        )
    except (FileNotFoundError, PermissionError) as e:
        action.verdict = "reject"
        action.verdict_reason = f"failed to spawn: {e}"
        action.returncode = 127
        action.finished_at = datetime.now()
        return (127, str(e), action)

    running = RunningTask(
        action_id=action.action_id,
        proc=proc,
        pid=proc.pid,
        command=list(argv),
        started_at=datetime.now(),
        step=step,
        task_entry=task_entry,
        log_file=log_path,
    )
    running_tasks[action.action_id] = running

    output_lines: list[str] = []
    try:
        with log_path.open("w") as log_f:
            assert proc.stdout is not None
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                text = line.decode(errors="replace")
                output_lines.append(text)
                log_f.write(text)
                log_f.flush()
                if step is not None:
                    step.output = "".join(output_lines)
                    try:
                        await step.update()
                    except Exception:
                        # best effort — if the session went away, keep executing
                        pass
        await proc.wait()
    finally:
        running_tasks.pop(action.action_id, None)

    combined_output = "".join(output_lines)
    action.returncode = proc.returncode
    action.finished_at = datetime.now()

    # Flip cl.Task status if the caller passed one
    if task_entry is not None:
        try:
            import chainlit as cl  # local import: module works without chainlit too

            task_entry.status = (
                cl.TaskStatus.DONE if proc.returncode == 0 else cl.TaskStatus.FAILED
            )
        except Exception:
            pass

    # Accounting — only on success
    if proc.returncode == 0:
        if action.classified_as == "write":
            budgets.increment_edits(1)
        if is_pipeline_script(argv):
            budgets.increment_tasks(1)

    _rotate_logs_if_needed()

    return (proc.returncode, combined_output, action)


# ---------- cancellation ----------


async def abort_task(action_id: str) -> tuple[bool, str]:
    """Cancel a running task via SIGTERM, with SIGKILL watchdog.

    Returns `(killed, message)` where `killed` is True if a task was
    actually signalled. `message` describes what happened for the UI.
    """
    task = running_tasks.get(action_id)
    if task is None:
        return (False, f"No running task with id {action_id}")

    pid = task.pid
    try:
        task.proc.terminate()
    except ProcessLookupError:
        return (True, f"Process {pid} already gone")

    try:
        await asyncio.wait_for(task.proc.wait(), timeout=SIGTERM_GRACE_SECONDS)
        return (
            True,
            f"Sent SIGTERM to pid {pid}, exited cleanly "
            f"(returncode {task.proc.returncode})",
        )
    except asyncio.TimeoutError:
        try:
            task.proc.kill()
        except ProcessLookupError:
            pass
        await task.proc.wait()
        return (
            True,
            f"Process pid {pid} did not respond to SIGTERM in "
            f"{SIGTERM_GRACE_SECONDS}s, sent SIGKILL",
        )


# ---------- snapshots (for sidebar / status / recent commands) ----------


def get_running_tasks_snapshot() -> list[dict]:
    """Plain-dict snapshot of running_tasks for the UI layer."""
    now = datetime.now()
    return [
        {
            "action_id": t.action_id,
            "pid": t.pid,
            "command": " ".join(t.command),
            "started_at": t.started_at.isoformat(),
            "runtime_seconds": (now - t.started_at).total_seconds(),
            "log_file": str(t.log_file) if t.log_file else None,
        }
        for t in running_tasks.values()
    ]


def get_recent_actions(n: int = 20) -> list[dict]:
    """Last N proposed actions with their outcomes."""
    return [
        {
            "action_id": a.action_id,
            "command": " ".join(a.command),
            "kind": a.kind,
            "classified_as": a.classified_as,
            "verdict": a.verdict,
            "verdict_reason": a.verdict_reason,
            "returncode": a.returncode,
            "proposed_at": a.proposed_at.isoformat(),
            "finished_at": a.finished_at.isoformat() if a.finished_at else None,
        }
        for a in action_log[-n:]
    ]


def clear_action_log() -> None:
    """Clear the in-memory action_log. Running tasks are unaffected."""
    action_log.clear()
