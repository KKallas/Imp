"""Workflow step: Fork a GitHub repository."""

import subprocess


def run(context):
    previous = context.get("previous_results", {})
    repo = previous.get("repo") or context.get("repo", "")
    if not repo:
        return {"ok": False, "output": "No repository specified (expected 'owner/repo')."}
    result = subprocess.run(
        ["python", "tools/github/fork.py", repo],
        capture_output=True, text=True,
    )
    output = result.stdout.strip()
    if result.stderr.strip():
        output = (output + "\n" + result.stderr.strip()).strip()
    return {
        "ok": result.returncode == 0,
        "output": output,
        "repo": repo,
    }
