"""Close a GitHub issue."""

import subprocess


def run(context):
    # Get issue number from previous step's output
    issue_number = None
    for prev in context.get("previous_results", []):
        if prev.get("issue_number"):
            issue_number = prev["issue_number"]
            break

    if not issue_number:
        return {"ok": False, "error": "No issue number found in previous steps"}

    result = subprocess.run(
        ["python", "tools/github/close_issue.py",
         str(issue_number),
         "--reason", "completed",
         "--comment", "Closed by workflow"],
        capture_output=True, text=True,
    )

    return {
        "ok": result.returncode == 0,
        "output": result.stdout.strip() or result.stderr.strip(),
    }
