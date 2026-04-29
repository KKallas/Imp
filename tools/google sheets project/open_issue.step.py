"""Workflow step: Create a new issue in Google Sheets project tracker."""

import re
import subprocess
from pathlib import Path

SCRIPT = str(Path(__file__).resolve().parent / "open_issue.py")


def run(context):
    previous = context.get("previous_results", {})

    title = context.get("title") or previous.get("title", "New Issue")
    body = context.get("body") or previous.get("body", "Created by workflow")
    labels = context.get("labels") or previous.get("labels", [])

    cmd = ["python", SCRIPT, "--title", title, "--body", body]
    for label in labels:
        cmd.extend(["--label", label])

    result = subprocess.run(cmd, capture_output=True, text=True)

    output = result.stdout.strip()

    # Parse issue number from "Created issue #N: ..."
    issue_number = None
    match = re.search(r"#(\d+)", output)
    if match:
        issue_number = int(match.group(1))

    return {
        "ok": result.returncode == 0,
        "output": output,
        "issue_number": issue_number,
        "issue_title": title,
    }
