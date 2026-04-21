#!/usr/bin/env python3
"""
Workflow step template for moderate_issues.

Calls moderate_issues.py via subprocess to find and moderate
GitHub issues that need formatting for Robot Arena.
"""

import subprocess
import sys
from pathlib import Path


def run(context):
    """
    Run the issue moderator tool as a workflow step.

    Args:
        context: Workflow context dict with keys:
            - previous_results: list of dicts from earlier steps
            - dry_run (optional): bool, if True run in dry-run mode
            - test (optional): bool, if True run in test mode
            - issue (optional): int, specific issue number to process
            - max_issues (optional): int, max issues to process
            - max_tokens (optional): int, token budget limit

    Returns:
        dict with keys: ok (bool), output (str), processed_count (int)
    """
    tools_dir = Path(__file__).resolve().parent
    script = tools_dir / "moderate_issues.py"

    if not script.exists():
        return {
            "ok": False,
            "output": f"Tool script not found: {script}",
            "processed_count": 0,
        }

    cmd = [sys.executable, str(script)]

    previous_results = context.get("previous_results", [])

    # Check if a previous step provided a specific issue number
    issue_number = context.get("issue")
    if not issue_number and previous_results:
        last = previous_results[-1]
        if isinstance(last, dict):
            issue_number = last.get("issue") or last.get("issue_number")

    if issue_number:
        cmd.extend(["--issue", str(issue_number)])

    if context.get("dry_run"):
        cmd.append("--dry-run")
    elif context.get("test"):
        cmd.append("--test")

    max_issues = context.get("max_issues")
    if max_issues:
        cmd.extend(["--max", str(max_issues)])

    max_tokens = context.get("max_tokens")
    if max_tokens:
        cmd.extend(["--max-tokens", str(max_tokens)])

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,
        )

        output = result.stdout
        if result.stderr:
            output += "\n[stderr]:\n" + result.stderr

        # Try to parse processed count from output
        processed_count = 0
        for line in output.splitlines():
            line_stripped = line.strip()
            if "Processed" in line_stripped and "/" in line_stripped:
                try:
                    part = line_stripped.split("Processed")[1].strip()
                    num = part.split("/")[0].strip()
                    processed_count = int(num)
                except (IndexError, ValueError):
                    pass
            elif "No issues need moderation" in line_stripped:
                processed_count = 0

        ok = result.returncode == 0

        return {
            "ok": ok,
            "output": output,
            "processed_count": processed_count,
        }

    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "output": "moderate_issues.py timed out after 600 seconds",
            "processed_count": 0,
        }
    except Exception as e:
        return {
            "ok": False,
            "output": f"Error running moderate_issues.py: {e}",
            "processed_count": 0,
        }
