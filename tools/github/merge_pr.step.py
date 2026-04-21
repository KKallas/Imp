"""Workflow step: Merge a pull request."""

import subprocess


def run(context):
    previous = context.get("previous_results", {})
    pr = previous.get("pr") or context.get("pr")
    method = previous.get("method") or context.get("method", "squash")
    repo = previous.get("repo") or context.get("repo")

    if not pr:
        return {"ok": False, "output": "No PR number provided"}

    cmd = ["python", "tools/github/merge_pr.py", str(pr), "--method", method]
    if repo:
        cmd.extend(["--repo", repo])

    result = subprocess.run(cmd, capture_output=True, text=True)
    output = result.stdout.strip()
    if result.stderr:
        output += "\n" + result.stderr.strip()

    return {
        "ok": result.returncode == 0,
        "output": output,
        "pr": pr,
        "method": method,
    }
