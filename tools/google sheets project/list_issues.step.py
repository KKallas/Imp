"""Workflow step: List issues from Google Sheets project tracker."""

import subprocess
from pathlib import Path

SCRIPT = str(Path(__file__).resolve().parent / "list_issues.py")


def run(context):
    previous_results = context.get("previous_results", {})

    cmd = ["python", SCRIPT]

    state = context.get("state", "open")
    cmd.extend(["--state", state])

    limit = context.get("limit", 30)
    cmd.extend(["--limit", str(limit)])

    labels = context.get("labels", [])
    for label in labels:
        cmd.extend(["--label", label])

    result = subprocess.run(cmd, capture_output=True, text=True)

    output = result.stdout.strip()
    error = result.stderr.strip()
    ok = result.returncode == 0

    return {
        "ok": ok,
        "output": output if ok else error,
        "issues_raw": output,
    }
