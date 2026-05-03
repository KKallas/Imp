#!/usr/bin/env python3
"""Post a text message to the Imp queue.

Inputs:
  --text: str — the text to post as the queue item detail.

Process: Sends a POST request to the local Imp queue API, creating
         a new queue item with the given text.
Output: Prints the created queue item JSON."""
import argparse
import json
import sys
import urllib.error
import urllib.request


def main() -> int:
    parser = argparse.ArgumentParser(description="Post text to the Imp queue.")
    parser.add_argument("--text", required=True, help="Text to post as queue item detail.")
    args = parser.parse_args()

    url = "http://127.0.0.1:8421/api/queue"
    payload = json.dumps({
        "title": "Test item",
        "detail_html": args.text,
        "tool": "imp_test_tool",
    }).encode()

    try:
        req = urllib.request.Request(url, method="POST", data=payload)
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            print(json.dumps(data, indent=2))
            return 0
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode()}", file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"Server not reachable: {e.reason}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
