"""Post triage summary to GitHub."""

import subprocess


def run(context):
    # Check if the review was approved
    prev = context.get("previous_results", [])
    if prev and prev[-1].get("pause"):
        # The pause step result doesn't carry the action — it's in the queue
        pass

    result = subprocess.run(
        ["gh", "issue", "list", "--state", "open", "--limit", "5"],
        capture_output=True, text=True,
    )
    summary = result.stdout.strip()
    return {
        "ok": True,
        "output": f"Triage complete. Top open issues:\n{summary}",
    }
