"""workflows — discovery + runner for step-based workflows.

Each workflow is a folder under ``workflows/`` with step scripts
(``step_*.py``) and a README. Steps run in filename order. A step
that returns ``{"pause": True, ...}`` pushes to the queue and waits.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import time
from pathlib import Path
from typing import Any

_WORKFLOWS_DIR = Path(__file__).parent


# ── discovery ───────────────────────────────────────────────────────

def discover() -> dict[str, Path]:
    """Return ``{name: path}`` for every workflow folder."""
    found: dict[str, Path] = {}
    for subdir in sorted(_WORKFLOWS_DIR.iterdir()):
        if not subdir.is_dir() or subdir.name.startswith(("_", ".")):
            continue
        steps = sorted(subdir.glob("step_*.py"))
        if steps:
            found[subdir.name] = subdir
    return found


def get_steps(name: str) -> list[dict[str, Any]]:
    """Return step metadata for a workflow."""
    path = _WORKFLOWS_DIR / name
    if not path.is_dir():
        return []
    steps = []
    for f in sorted(path.glob("step_*.py")):
        # Read docstring as description
        desc = ""
        try:
            src = f.read_text()
            for line in src.splitlines():
                line = line.strip()
                if line.startswith('"""') or line.startswith("'''"):
                    desc = line.strip('"').strip("'").strip()
                    break
        except Exception:
            pass
        steps.append({
            "name": f.stem,
            "file": str(f),
            "description": desc,
            "source": src,
        })
    return steps


def get_readme(name: str) -> str:
    """Return README content for a workflow, or empty string."""
    readme = _WORKFLOWS_DIR / name / "README.md"
    if readme.exists():
        return readme.read_text()
    return ""


# ── runner ──────────────────────────────────────────────────────────

# Active runners keyed by workflow name
_runners: dict[str, WorkflowRunner] = {}


class WorkflowRunner:
    """Runs a workflow's steps in order, pausing when a step requests it."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.steps = get_steps(name)
        self.current = 0
        self.status = "idle"  # idle | running | paused | done | error
        self.results: list[dict[str, Any]] = []
        self.pause_item_id: str | None = None
        self._resume_event = asyncio.Event()

    def to_dict(self) -> dict[str, Any]:
        step_statuses = []
        for i, step in enumerate(self.steps):
            if i < self.current:
                s = "done"
            elif i == self.current and self.status == "running":
                s = "running"
            elif i == self.current and self.status == "paused":
                s = "paused"
            else:
                s = "pending"
            step_statuses.append({
                "name": step["name"],
                "description": step["description"],
                "source": step.get("source", ""),
                "status": s,
                "result": self.results[i] if i < len(self.results) else None,
            })
        return {
            "name": self.name,
            "status": self.status,
            "current_step": self.current,
            "total_steps": len(self.steps),
            "steps": step_statuses,
            "ran_at": None,
        }

    async def run(self) -> None:
        """Execute all steps in order."""
        self.status = "running"
        for i, step_meta in enumerate(self.steps):
            self.current = i
            self.status = "running"

            result = await self._run_step(step_meta)
            self.results.append(result)

            # Stop workflow on step failure
            if not result.get("ok", True) and not result.get("pause"):
                self.status = "error"
                self._save_log()
                return

            # Handle pause
            if result.get("pause"):
                self.status = "paused"
                await self._push_to_queue(result)
                await self._resume_event.wait()
                self._resume_event.clear()
                self.pause_item_id = None

        self.current = len(self.steps)
        self.status = "done"
        self._save_log()

    async def _run_step(self, step_meta: dict[str, Any]) -> dict[str, Any]:
        """Import and run a step's run() function."""
        file_path = step_meta["file"]
        spec = importlib.util.spec_from_file_location(step_meta["name"], file_path)
        if spec is None or spec.loader is None:
            return {"ok": False, "error": f"Cannot load {file_path}"}
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        run_fn = getattr(module, "run", None)
        if run_fn is None:
            return {"ok": False, "error": f"No run() function in {file_path}"}

        context = {
            "workflow": self.name,
            "step": step_meta["name"],
            "previous_results": list(self.results),
        }

        t0 = time.monotonic()
        try:
            if asyncio.iscoroutinefunction(run_fn):
                result = await run_fn(context)
            else:
                result = run_fn(context)
        except Exception as exc:
            import traceback
            result = {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
            }
        duration = time.monotonic() - t0

        if not isinstance(result, dict):
            result = {"ok": True, "output": str(result)}
        result["duration_s"] = round(duration, 2)
        return result

    async def _push_to_queue(self, result: dict[str, Any]) -> None:
        """Push a pause item to the work queue."""
        from server import work_queue

        item = work_queue.add(
            tool=f"workflow:{self.name}",
            title=result.get("title", f"{self.name} — paused"),
            detail_html=result.get("detail_html", ""),
            actions=result.get("actions", [{"label": "Continue", "action": "continue"}]),
        )
        self.pause_item_id = item["id"]

    def _save_log(self) -> None:
        """Save run results to disk alongside the workflow."""
        import json
        from datetime import datetime, timezone
        log_path = _WORKFLOWS_DIR / self.name / "last_run.json"
        log = {
            "status": self.status,
            "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "steps": [],
        }
        for i, step in enumerate(self.steps):
            log["steps"].append({
                "name": step["name"],
                "description": step["description"],
                "result": self.results[i] if i < len(self.results) else None,
            })
        log_path.write_text(json.dumps(log, indent=2))

    @staticmethod
    def load_last_run(name: str) -> dict[str, Any] | None:
        """Load the last run log from disk."""
        import json
        log_path = _WORKFLOWS_DIR / name / "last_run.json"
        if not log_path.exists():
            return None
        try:
            return json.loads(log_path.read_text())
        except (json.JSONDecodeError, KeyError):
            return None

    def resume(self) -> None:
        """Resume from a pause (called when queue item is resolved)."""
        self._resume_event.set()

    def abort(self) -> None:
        """Cancel the workflow."""
        self.status = "error"
        self._resume_event.set()  # unblock if paused


# ── public API ──────────────────────────────────────────────────────

def start(name: str) -> WorkflowRunner | None:
    """Start a workflow. Returns the runner, or None if not found."""
    if name not in discover():
        return None
    if name in _runners and _runners[name].status in ("running", "paused"):
        return _runners[name]  # already running
    runner = WorkflowRunner(name)
    _runners[name] = runner
    asyncio.create_task(runner.run())
    return runner


def get_runner(name: str) -> WorkflowRunner | None:
    return _runners.get(name)


def list_runners() -> dict[str, dict[str, Any]]:
    return {name: r.to_dict() for name, r in _runners.items()}
