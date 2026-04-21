"""Post triage summary to GitHub."""

import subprocess
from datetime import datetime


def run(context):
    prev = context.get("previous_results", [])

    # Step 4 is the review/pause step — check if approved
    if len(prev) >= 4:
        review_result = prev[3]
        if review_result.get("pause") and not review_result.get("approved", True):
            return {
                "ok": False,
                "output": "Triage summary not posted — review was not approved.",
            }

    # Gather data from previous steps
    issues_data = prev[0] if len(prev) >= 1 else {}
    heuristics_data = prev[1] if len(prev) >= 2 else {}
    burndown_data = prev[2] if len(prev) >= 3 else {}

    # Build the summary body
    date_str = datetime.now().strftime("%Y-%m-%d")

    summary_lines = [f"## Weekly Triage Summary — {date_str}\n"]

    issue_count = issues_data.get("issue_count", 0)
    summary_lines.append(f"**Issues synced:** {issue_count}")

    if heuristics_data.get("issues"):
        summary_lines.append("\n### Heuristics\n")
        for issue in heuristics_data.get("issues", []):
            number = issue.get("issue_number", "?")
            title = issue.get("issue_title", "Untitled")
            duration = issue.get("estimated_duration", "unknown")
            deps = issue.get("dependencies", [])
            dep_str = ", ".join(f"#{d}" for d in deps) if deps else "none"
            summary_lines.append(
                f"- #{number} **{title}** — est. {duration}, deps: {dep_str}"
            )

    if burndown_data.get("chart_path"):
        summary_lines.append(f"\n**Burndown chart:** {burndown_data.get('chart_path')}")
    if burndown_data.get("output"):
        summary_lines.append(f"\n{burndown_data.get('output')}")

    summary_body = "\n".join(summary_lines)

    # Post the triage summary as a new GitHub issue
    title = f"Weekly Triage Summary — {date_str}"
    result = subprocess.run(
        [
            "gh", "issue", "create",
            "--title", title,
            "--body", summary_body,
            "--label", "triage",
        ],
        capture_output=True, text=True,
    )

    if result.returncode != 0:
        return {
            "ok": False,
            "output": f"Failed to post triage summary: {result.stderr.strip()}",
        }

    issue_url = result.stdout.strip()
    return {
        "ok": True,
        "output": f"Triage complete. Summary posted: {issue_url}\n\n{summary_body}",
        "issue_url": issue_url,
        "summary": summary_body,
    }
