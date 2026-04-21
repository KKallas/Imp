"""Sync issues from GitHub."""

import json
import subprocess


def run(context):
    result = subprocess.run(
        ["python", "pipeline/sync_issues.py"],
        capture_output=True, text=True,
    )
    summary = "sync failed"
    issues = []
    try:
        data = json.loads(result.stdout)
        issues = data.get("issues", [])
        count = data.get("issue_count", len(issues))
        repo = data.get("repo", "")
        summary = f"Synced {count} issues from {repo}"
    except (json.JSONDecodeError, TypeError):
        summary = result.stderr.strip()[:500] or result.stdout[:500]

    return {
        "ok": result.returncode == 0,
        "output": summary,
        "issues": issues,
    }
