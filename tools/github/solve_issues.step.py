#!/usr/bin/env python3
"""Workflow step template for solve_issues tool.

Fetch issues labeled "llm-ready" from GitHub, solve each with Claude Code,
and open PRs with the fixes.
"""

import subprocess
import sys
from pathlib import Path


TOOL_SCRIPT = str(Path(__file__).resolve().parent / "solve_issues.py")


def run(context: dict) -> dict:
    """Run the solve_issues tool as a workflow step.

    Args:
        context: Workflow context dict. Keys used:
            - previous_results (list[dict]): results from earlier steps.
            - dry_run (bool): if True, pass --dry-run.
            - test (bool): if True, pass --test.
            - issue (int|None): specific issue number to process.
            - max (int|None): max issues to process.
            - max_tokens (int|None): cumulative token budget.

    Returns:
        dict with keys: ok (bool), output (str), solved (int), total (int).
    """
    previous_results = context.get("previous_results", [])

    cmd = [sys.executable, TOOL_SCRIPT]

    # Check previous step results for an issue number to forward
    issue_from_previous = None
    for prev in previous_results:
        if isinstance(prev, dict) and prev.get("issue"):
            issue_from_previous = prev["issue"]
            break

    # Build CLI args from context
    if context.get("dry_run"):
        cmd.append("--dry-run")

    if context.get("test"):
        cmd.append("--test")

    issue_number = context.get("issue") or issue_from_previous
    if issue_number is not None:
        cmd.extend(["--issue", str(issue_number)])

    max_issues = context.get("max")
    if max_issues is not None:
        cmd.extend(["--max", str(max_issues)])

    max_tokens = context.get("max_tokens")
    if max_tokens is not None:
        cmd.extend(["--max-tokens", str(max_tokens)])

    result = subprocess.run(cmd, capture_output=True, text=True)
    output = result.stdout + result.stderr
    ok = result.returncode == 0

    # Parse summary from output
    solved = 0
    total = 0
    for line in output.splitlines():
        line_stripped = line.strip()
        if line_stripped.startswith("Done! Solved"):
            # e.g. "Done! Solved 3/5 issues"
            try:
                parts = line_stripped.split("Solved")[1].strip().split("/")
                solved = int(parts[0].strip())
                total = int(parts[1].split()[0].strip())
            except (IndexError, ValueError):
                pass
        elif "completed!" in line_stripped:
            solved += 1

    return {
        "ok": ok,
        "output": output,
        "solved": solved,
        "total": total,
    }
