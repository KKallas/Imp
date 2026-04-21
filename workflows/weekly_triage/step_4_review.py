"""Review the burndown chart before posting."""


def run(context):
    chart = ""
    chart_path = ""
    prev = context.get("previous_results", [])
    if prev and len(prev) >= 3:
        step3_result = prev[2]
        if step3_result.get("output"):
            chart_path = step3_result["output"]
            chart = f'<p>Chart at: <code>{chart_path}</code></p>'

    return {
        "pause": True,
        "title": "Review burndown chart",
        "detail_html": (
            "<h3>Weekly Triage — Step 4</h3>"
            "<p>The burndown chart has been generated. Review it before "
            "posting the summary to GitHub.</p>"
            f"{chart}"
            '<p><a href="/render/burndown?mode=viewer" target="_blank">'
            "Open interactive chart</a></p>"
        ),
        "actions": [
            {"label": "Approve & Post", "action": "approve"},
            {"label": "Skip Posting", "action": "skip"},
        ],
        "ok": True,
        "output": chart_path if chart_path else "Burndown chart ready for review",
        "approved": None,
    }
