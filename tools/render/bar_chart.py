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
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
  body {{ margin:0; padding:16px; background:#0d1117; color:#c9d1d9; font-family:sans-serif; }}
  .chart-container {{ background:#161b22; border:1px solid #30363d; border-radius:8px; padding:16px; }}
  h3 {{ margin:0 0 12px; font-size:14px; color:#c9d1d9; }}
  canvas {{ max-height: 400px; }}
</style>
</head>
<body>
<div class="chart-container">
  <h3>{title}</h3>
  <canvas id="chart"></canvas>
</div>
<script>
  var colors = ['#58a6ff', '#3fb950', '#d29922', '#f85149', '#a371f7', '#79c0ff'];
  var chartData = {data_json};
  new Chart(document.getElementById('chart'), {{
    type: '{chart_type}',
    data: {{
      labels: chartData.labels,
      datasets: chartData.datasets.map(function(ds, i) {{
        return {{
          label: ds.name,
          data: ds.values,
          borderColor: colors[i % colors.length],
          backgroundColor: colors[i % colors.length] + '33',
          borderWidth: 2,
          tension: 0.3
        }};
      }})
    }},
    options: {{
      responsive: true,
      plugins: {{ legend: {{ labels: {{ color: '#c9d1d9' }} }} }},
      scales: {{
        x: {{ ticks: {{ color: '#8b949e' }}, grid: {{ color: '#21262d' }} }},
        y: {{ ticks: {{ color: '#8b949e' }}, grid: {{ color: '#21262d' }} }}
      }}
    }}
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

    # Save as artifact file with unique name
    artifact_id = hashlib.md5(f"{args.title}{time.time()}".encode()).hexdigest()[:12]
    artifact_dir = Path("public/charts")
    artifact_dir.mkdir(parents=True, exist_ok=True)
    html_path = artifact_dir / f"{artifact_id}.html"
    html_path.write_text(html)

    base = f"http://127.0.0.1:{args.port}"

    chart_url = f"{base}/public/charts/{artifact_id}.html"
    png_url = f"{base}/renderpng?src=/public/charts/{artifact_id}.html"
    print(f"[Open in dashboard]({chart_url})")
    print(f"[Download PNG]({png_url})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
