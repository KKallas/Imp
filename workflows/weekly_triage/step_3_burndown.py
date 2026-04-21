"""Generate burndown chart."""

import subprocess
import json
from datetime import datetime


def run(context):
    previous_results = context.get("previous_results", [])

    # Step 2 results contain heuristics with durations and dependencies
    heuristics_result = previous_results[1] if len(previous_results) > 1 else {}
    # Step 1 results contain synced issues
    sync_result = previous_results[0] if len(previous_results) > 0 else {}

    issues = sync_result.get("issues", [])
    durations = heuristics_result.get("durations", {})
    dependencies = heuristics_result.get("dependencies", {})

    # Build burndown data from issues and heuristics
    burndown_data = []
    for issue in issues:
        issue_number = issue.get("issue_number")
        burndown_data.append({
            "issue_number": issue_number,
            "issue_title": issue.get("issue_title", ""),
            "state": issue.get("state", "open"),
            "estimated_duration": durations.get(str(issue_number), durations.get(issue_number, 1)),
            "dependencies": dependencies.get(str(issue_number), dependencies.get(issue_number, [])),
        })

    today = datetime.now().strftime("%Y-%m-%d")

    result = subprocess.run(
        ["python", "-m", "renderers.helpers", "--template", "burndown",
         "--date", today,
         "--data", json.dumps(burndown_data)],
        capture_output=True, text=True,
    )
    chart_path = result.stdout.strip()
    return {
        "ok": result.returncode == 0,
        "output": chart_path,
        "chart_path": chart_path,
        "burndown_data": burndown_data,
        "generated_date": today,
    }
