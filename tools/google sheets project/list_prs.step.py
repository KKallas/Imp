"""Workflow step: List pull requests from Google Sheets project tracker."""

import subprocess
from pathlib import Path

SCRIPT = str(Path(__file__).resolve().parent / "list_prs.py")


def run(context):
    previous_results = context.get("previous_results", {})

    state = context.get("state") or previous_results.get("state", "open")
    limit = context.get("limit") or previous_results.get("limit", 30)

    cmd = ["python", SCRIPT, "--state", state, "--limit", str(limit)]

    result = subprocess.run(cmd, capture_output=True, text=True)

    output = result.stdout.strip()
    prs = []
    for line in output.splitlines():
        if line.strip() and not line.startswith("-") and not line.startswith("ID"):
            prs.append(line.strip())

    return {
        "ok": result.returncode == 0,
        "output": output,
        "prs": prs,
        "count": len(prs),
    }
