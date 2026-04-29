#!/usr/bin/env python3
"""Render an interactive table and push it to the dashboard.

Inputs:
  --data: str — JSON data in DataFrame format: {"columns": [...], "data": [[...], ...]}
  --title: str — table title (default: "Table").
  --port: int — server port (default: 8421).

Process: Generates sortable/filterable HTML table, pushes to dashboard.
Output: Prints confirmation."""
import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request

TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body {{ margin:0; padding:16px; background:#0d1117; color:#c9d1d9; font-family:sans-serif; }}
  h3 {{ margin:0 0 12px; font-size:14px; }}
  .table-wrap {{ background:#161b22; border:1px solid #30363d; border-radius:8px; overflow:auto; }}
  input {{ width:100%; padding:8px 12px; background:#0d1117; color:#c9d1d9; border:none;
    border-bottom:1px solid #30363d; font-size:13px; outline:none; box-sizing:border-box; }}
  input:focus {{ border-bottom-color:#58a6ff; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  th {{ background:#161b22; padding:8px 12px; text-align:left; cursor:pointer;
    border-bottom:1px solid #30363d; font-weight:600; user-select:none; color:#8b949e; }}
  th:hover {{ color:#c9d1d9; }}
  td {{ padding:6px 12px; border-bottom:1px solid #21262d; }}
  tr:hover td {{ background:#1c2128; }}
  .sort-arrow {{ font-size:10px; margin-left:4px; }}
</style>
</head>
<body>
<h3>{title}</h3>
<div class="table-wrap">
  <input type="text" id="filter" placeholder="Filter..." oninput="filterTable()">
  <table>
    <thead><tr id="header"></tr></thead>
    <tbody id="body"></tbody>
  </table>
</div>
<script>
var columns = {columns_json};
var data = {data_json};
var sortCol = -1, sortAsc = true;

function render() {{
  var h = document.getElementById('header');
  h.innerHTML = columns.map(function(c, i) {{
    var arrow = sortCol === i ? (sortAsc ? ' \\u25B2' : ' \\u25BC') : '';
    return '<th onclick="sortBy(' + i + ')">' + c + '<span class="sort-arrow">' + arrow + '</span></th>';
  }}).join('');

  var filter = (document.getElementById('filter').value || '').toLowerCase();
  var rows = data;
  if (filter) {{
    rows = data.filter(function(r) {{
      return r.some(function(c) {{ return String(c).toLowerCase().includes(filter); }});
    }});
  }}

  document.getElementById('body').innerHTML = rows.map(function(r) {{
    return '<tr>' + r.map(function(c) {{ return '<td>' + (c === null ? '' : c) + '</td>'; }}).join('') + '</tr>';
  }}).join('');
}}

function sortBy(col) {{
  if (sortCol === col) {{ sortAsc = !sortAsc; }} else {{ sortCol = col; sortAsc = true; }}
  data.sort(function(a, b) {{
    var va = a[col], vb = b[col];
    if (va === null) return 1; if (vb === null) return -1;
    if (typeof va === 'number' && typeof vb === 'number') return sortAsc ? va - vb : vb - va;
    return sortAsc ? String(va).localeCompare(String(vb)) : String(vb).localeCompare(String(va));
  }});
  render();
}}

function filterTable() {{ render(); }}
render();
</script>
</body>
</html>"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Render a table to the dashboard")
    parser.add_argument("--data", required=True, help='JSON: {"columns": [...], "data": [[...], ...]}')
    parser.add_argument("--title", default="Table")
    parser.add_argument("--port", type=int, default=8421)
    args = parser.parse_args()

    try:
        raw = json.loads(args.data)
    except json.JSONDecodeError as e:
        print(f"Invalid JSON data: {e}", file=sys.stderr)
        return 1

    columns = raw.get("columns", [])
    data = raw.get("data", [])

    html = TEMPLATE.format(
        title=args.title,
        columns_json=json.dumps(columns),
        data_json=json.dumps(data),
    )

    from pathlib import Path
    artifact_id = hashlib.md5(f"{args.title}{time.time()}".encode()).hexdigest()[:12]
    artifact_dir = Path("public/charts")
    artifact_dir.mkdir(parents=True, exist_ok=True)
    html_path = artifact_dir / f"{artifact_id}.html"
    html_path.write_text(html)

    base = f"http://127.0.0.1:{args.port}"
    print(f"[Open in dashboard]({base}/public/charts/{artifact_id}.html)")
    print(f"[Download PNG]({base}/public/charts/{artifact_id}.html)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
