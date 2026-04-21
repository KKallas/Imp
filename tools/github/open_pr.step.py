"""Workflow step: Open a pull request."""

import subprocess


def run(context):
    previous = context.get("previous_results", {})

    title = previous.get("pr_title", "")
    body = previous.get("pr_body", "")
    base = previous.get("base_branch")
    head = previous.get("head_branch")
    repo = previous.get("repo")

    if not title:
        return {"ok": False, "output": "Missing required 'pr_title' in previous_results"}

    cmd = ["python", "tools/github/open_pr.py", "--title", title, "--body", body]
    if base:
        cmd.extend(["--base", base])
    if head:
        cmd.extend(["--head", head])
    if repo:
        cmd.extend(["--repo", repo])

    result = subprocess.run(cmd, capture_output=True, text=True)
    output = result.stdout.strip()
    if result.stderr:
        output += "\n" + result.stderr.strip()

    pr_url = ""
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("https://"):
            pr_url = line
            break

    return {
        "ok": result.returncode == 0,
        "output": output,
        "pr_url": pr_url,
    }
