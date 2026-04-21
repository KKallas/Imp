"""Workflow template for listing pull requests."""

import subprocess


def run(context):
    previous_results = context.get("previous_results", {})

    repo = previous_results.get("repo")
    state = previous_results.get("state", "open")
    limit = previous_results.get("limit", 30)

    cmd = ["python", "tools/github/list_prs.py", "--state", state, "--limit", str(limit)]
    if repo:
        cmd.extend(["--repo", repo])

    result = subprocess.run(cmd, capture_output=True, text=True)

    output = result.stdout.strip()
    prs = []
    for line in output.splitlines():
        if line.strip():
            prs.append(line.strip())

    return {
        "ok": result.returncode == 0,
        "output": output,
        "prs": prs,
        "count": len(prs),
    }
