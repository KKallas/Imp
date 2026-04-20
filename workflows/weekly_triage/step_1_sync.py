"""Sync issues from GitHub."""

import subprocess


def run(context):
    result = subprocess.run(
        ["python", "pipeline/sync_issues.py"],
        capture_output=True, text=True,
    )
    return {
        "ok": result.returncode == 0,
        "output": result.stdout[:2000],
    }
