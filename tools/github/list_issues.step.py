"""Workflow step: List GitHub issues."""

import subprocess


def run(context):
    previous_results = context.get("previous_results", {})

    cmd = ["python", "tools/github/list_issues.py"]

    state = context.get("state", "open")
    cmd.extend(["--state", state])

    limit = context.get("limit", 30)
    cmd.extend(["--limit", str(limit)])

    labels = context.get("labels", [])
    for label in labels:
        cmd.extend(["--label", label])

    repo = context.get("repo") or previous_results.get("repo")
    if repo:
        cmd.extend(["--repo", repo])

    result = subprocess.run(cmd, capture_output=True, text=True)

    output = result.stdout.strip()
    error = result.stderr.strip()
    ok = result.returncode == 0

    return {
        "ok": ok,
        "output": output if ok else error,
        "issues_raw": output,
    }
