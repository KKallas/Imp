"""Workflow step: Create a pull request in Google Sheets project tracker."""

import re
import subprocess
from pathlib import Path

SCRIPT = str(Path(__file__).resolve().parent / "open_pr.py")


def run(context):
    previous = context.get("previous_results", {})

    title = context.get("pr_title") or previous.get("pr_title", "")
    body = context.get("pr_body") or previous.get("pr_body", "")
    base = context.get("base_branch") or previous.get("base_branch")
    head = context.get("head_branch") or previous.get("head_branch")

    if not title:
        return {"ok": False, "output": "Missing required 'pr_title' in context"}

    cmd = ["python", SCRIPT, "--title", title, "--body", body]
    if base:
        cmd.extend(["--base", base])
    if head:
        cmd.extend(["--head", head])

    result = subprocess.run(cmd, capture_output=True, text=True)
    output = result.stdout.strip()
    if result.stderr:
        output += "\n" + result.stderr.strip()

    # Parse PR number from "Created PR #N: ..."
    pr_number = None
    match = re.search(r"#(\d+)", output)
    if match:
        pr_number = int(match.group(1))

    return {
        "ok": result.returncode == 0,
        "output": output,
        "pr_number": pr_number,
    }
