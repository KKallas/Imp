#!/usr/bin/env python3
"""Embed a dashboard widget in the chat.

Inputs:
  widget (positional): Widget type (video, iframe, or any custom widget in static/widgets/).
  --param KEY=VALUE: Parameters passed to the widget template (repeatable).
  --width: str — widget width (default: 100%).
  --height: str — widget height (default: 400px).
  --port: int — server port (default: 8421).

Process: Builds a widget URL and prints an embeddable iframe tag.
Output: Prints HTML that the chat UI can render inline."""
import argparse
import sys
from urllib.parse import urlencode


def main() -> int:
    parser = argparse.ArgumentParser(description="Embed a dashboard widget")
    parser.add_argument("widget", help="Widget type (video, iframe, etc.)")
    parser.add_argument("--param", action="append", default=[], help="KEY=VALUE (repeatable)")
    parser.add_argument("--width", default="100%", help="Widget width")
    parser.add_argument("--height", default="400px", help="Widget height")
    parser.add_argument("--port", type=int, default=8421)
    args = parser.parse_args()

    params = {}
    for p in args.param:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k] = v

    base = f"http://127.0.0.1:{args.port}"
    qs = urlencode(params) if params else ""
    url = f"{base}/dashboard/{args.widget}" + (f"?{qs}" if qs else "")

    print(f"Widget URL: {url}")
    print(f'<iframe src="{url}" style="width:{args.width};height:{args.height};border:1px solid #333;border-radius:6px;" allowfullscreen></iframe>')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
