#!/usr/bin/env python3
"""Render a bar chart and push it to the dashboard.

Inputs:
  --data: str — JSON data in DataFrame format: {"labels": [...], "datasets": [{"name": "...", "values": [...]}]}
  --title: str — chart title (default: "Chart").
  --type: str — chart type: bar, line, pie, percentage (default: bar).
  --port: int — server port (default: 8421).

Process: Generates self-contained HTML using Frappe Charts, pushes to dashboard.
Output: Prints confirmation."""
import argparse
import json
import sys
import urllib.error
import urllib.request

TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<script src="https://cdn.jsdelivr.net/npm/frappe-charts@2/dist/frappe-charts.umd.min.js"></script>
<style>
  body {{ margin:0; padding:16px; background:#0d1117; color:#c9d1d9; font-family:sans-serif; }}
  .chart-container {{ background:#161b22; border:1px solid #30363d; border-radius:8px; padding:16px; }}
  .chart-container .title {{ display:none; }}
  h3 {{ margin:0 0 12px; font-size:14px; color:#c9d1d9; }}
</style>
</head>
<body>
<div class="chart-container">
  <h3>{title}</h3>
  <div id="chart"></div>
</div>
<script>
  new frappe.Chart("#chart", {{
    data: {data_json},
    type: '{chart_type}',
    height: 300,
    colors: ['#58a6ff', '#3fb950', '#d29922', '#f85149', '#a371f7', '#79c0ff'],
    barOptions: {{ spaceRatio: 0.4 }},
    axisOptions: {{ xAxisMode: 'tick', xIsSeries: true }},
  }});
</script>
</body>
</html>"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Render a chart to the dashboard")
    parser.add_argument("--data", required=True, help='JSON: {"labels": [...], "datasets": [{"name": "...", "values": [...]}]}')
    parser.add_argument("--title", default="Chart")
    parser.add_argument("--type", default="bar", choices=["bar", "line", "pie", "percentage"])
    parser.add_argument("--port", type=int, default=8421)
    args = parser.parse_args()

    try:
        data = json.loads(args.data)
    except json.JSONDecodeError as e:
        print(f"Invalid JSON data: {e}", file=sys.stderr)
        return 1

    html = TEMPLATE.format(
        title=args.title,
        data_json=json.dumps(data),
        chart_type=args.type,
    )

    # Push to dashboard
    base = f"http://127.0.0.1:{args.port}"
    try:
        req = urllib.request.Request(
            f"{base}/api/dashboard",
            data=json.dumps({"html": html}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            json.loads(resp.read().decode())
        print(f"Chart '{args.title}' pushed to dashboard. The dashboard will refresh automatically.")
        return 0
    except Exception as e:
        print(f"Failed to push to dashboard: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
