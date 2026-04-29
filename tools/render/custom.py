#!/usr/bin/env python3
"""Push custom HTML to the dashboard.

Inputs:
  --html: str — raw HTML string to display.
  --file: str — path to an HTML file to load (alternative to --html).
  --port: int — server port (default: 8421).

Process: Wraps the HTML in a dark-themed page shell with postMessage callback
         support, pushes to dashboard.
Output: Prints confirmation.

The HTML can include interactive elements. To send events back to the chat:
  window.parent.postMessage({type: 'widget_event', action: 'clicked', value: 42}, '*');
"""
import argparse
import json
import sys
import urllib.error
import urllib.request

SHELL = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body {{ margin:0; padding:16px; background:#0d1117; color:#c9d1d9; font-family:sans-serif; }}
  button {{ background:#58a6ff; color:#fff; border:none; border-radius:4px;
    padding:6px 14px; cursor:pointer; font-size:13px; }}
  button:hover {{ opacity:0.9; }}
</style>
</head>
<body>
{content}
</body>
</html>"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Push custom HTML to dashboard")
    parser.add_argument("--html", default="", help="Raw HTML string")
    parser.add_argument("--file", default="", help="Path to HTML file")
    parser.add_argument("--port", type=int, default=8421)
    args = parser.parse_args()

    if args.file:
        try:
            content = open(args.file).read()
        except Exception as e:
            print(f"Cannot read file: {e}", file=sys.stderr)
            return 1
    elif args.html:
        content = args.html
    else:
        print("Provide --html or --file", file=sys.stderr)
        return 1

    # If it's a full HTML doc, use as-is; otherwise wrap in shell
    if "<html" in content.lower():
        html = content
    else:
        html = SHELL.format(content=content)

    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{args.port}/api/dashboard",
            data=json.dumps({"html": html}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read().decode())
            print(f"Custom HTML pushed to dashboard ({result.get('length', '?')} chars)")
            return 0
    except Exception as e:
        print(f"Failed to push to dashboard: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
