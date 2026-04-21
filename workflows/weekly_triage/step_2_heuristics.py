"""Run heuristics to infer durations and dependencies."""

import subprocess
import json


def run(context):
    previous_results = context.get("previous_results", [])
    step1_result = previous_results[0] if previous_results else {}

    issues = step1_result.get("issues", [])

    issue_numbers = [str(issue.get("issue_number", "")) for issue in issues if issue.get("issue_number")]

    cmd = ["python", "pipeline/heuristics.py"]
    if issue_numbers:
        cmd.extend(["--issues", ",".join(issue_numbers)])

    result = subprocess.run(
        cmd,
        capture_output=True, text=True,
    )

    enriched_issues = []
    try:
        enriched_issues = json.loads(result.stdout.strip())
    except (json.JSONDecodeError, ValueError):
        pass

    if not enriched_issues and issues:
        enriched_issues = []
        for issue in issues:
            enriched_issue = dict(issue)
            enriched_issue.setdefault("estimated_duration", 1)
            enriched_issue.setdefault("dependencies", [])
            enriched_issue.setdefault("priority", "medium")
            enriched_issues.append(enriched_issue)

    summary = result.stderr.strip().split("\n")[-1] if result.stderr.strip() else "Heuristics complete"

    return {
        "ok": result.returncode == 0,
        "output": summary,
        "enriched_issues": enriched_issues,
        "total_issues": len(enriched_issues),
    }
