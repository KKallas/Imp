"""Review the burndown chart before posting."""


def run(context):
    chart = ""
    prev = context.get("previous_results", [])
    if prev and prev[-1].get("output"):
        chart = f'<p>Chart at: <code>{prev[-1]["output"]}</code></p>'

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
    }
