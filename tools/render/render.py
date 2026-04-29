#!/usr/bin/env python3
"""Render a chart or diagram via the Imp render server.

Inputs:
  type (positional): Renderer name (mermaid, plotly, gantt, kanban, bar, scatter, burndown, comparison).
  --param KEY=VALUE: Parameters for the renderer (repeatable).
  --port: int — server port (default: 8421).

Process: Builds a render URL from the type and parameters.
Output: Prints the image URL and viewer URL for embedding in chat."""
import argparse
import sys
from urllib.parse import quote, urlencode


def main() -> int:
    parser = argparse.ArgumentParser(description="Render a chart or diagram")
    parser.add_argument("type", help="Renderer name (mermaid, plotly, gantt, etc.)")
    parser.add_argument("--param", action="append", default=[], help="KEY=VALUE parameter (repeatable)")
    parser.add_argument("--port", type=int, default=8421)
    args = parser.parse_args()

    params = {}
    for p in args.param:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k] = v
        else:
            print(f"Invalid param (need KEY=VALUE): {p}", file=sys.stderr)
            return 1

    base = f"http://127.0.0.1:{args.port}"
    qs = urlencode(params, doseq=True)
    image_url = f"{base}/render/{args.type}?{qs}"
    viewer_url = f"{base}/render/{args.type}?{qs}&mode=viewer"

    print(f"Image: {image_url}")
    print(f"Viewer: {viewer_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
