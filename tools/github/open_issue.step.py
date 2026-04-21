"""Create a new GitHub issue."""

import json
import subprocess
from datetime import datetime


def run(context):
    title = "New Issue"
    body = "Created by workflow"

    result = subprocess.run(
        ["python", "tools/github/open_issue.py",
         "--title", title,
         "--body", body],
        capture_output=True, text=True,
    )

    # Parse issue number from output URL
    output = result.stdout.strip()
    issue_number = None
    for line in output.splitlines():
        if "/issues/" in line:
            try:
                issue_number = int(line.strip().rstrip("/").split("/")[-1])
            except ValueError:
                pass

    return {
        "ok": result.returncode == 0,
        "output": output,
        "issue_number": issue_number,
        "issue_title": title,
    }
