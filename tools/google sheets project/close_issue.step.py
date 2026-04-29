"""Workflow step: Close an issue in Google Sheets project tracker."""

import subprocess
from pathlib import Path

SCRIPT = str(Path(__file__).resolve().parent / "close_issue.py")


def run(context):
    # Get issue number from previous step's output
    issue_number = context.get("issue_number")
    if not issue_number:
        for prev in context.get("previous_results", []):
            if isinstance(prev, dict) and prev.get("issue_number"):
                issue_number = prev["issue_number"]
                break

    if not issue_number:
        return {"ok": False, "error": "No issue number found in previous steps"}

    reason = context.get("reason", "completed")
    comment = context.get("comment", "Closed by workflow")

    cmd = ["python", SCRIPT, str(issue_number), "--reason", reason]
    if comment:
        cmd.extend(["--comment", comment])

    result = subprocess.run(cmd, capture_output=True, text=True)

    return {
        "ok": result.returncode == 0,
        "output": result.stdout.strip() or result.stderr.strip(),
    }
