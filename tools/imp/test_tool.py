#!/usr/bin/env python3
"""Add a test item to the Imp queue.

Inputs: --text <string>  Content for the queue item.

Process: Posts a new item to the queue API with the given text as detail.
Output: Prints the created queue item JSON."""
import argparse
import json
import sys
import urllib.error
import urllib.request


def main() -> int:
    parser = argparse.ArgumentParser(description="Add a test item to the queue.")
    parser.add_argument("--text", required=True, help="Content for the queue item.")
    args = parser.parse_args()

    url = "http://127.0.0.1:8421/api/queue"
    payload = json.dumps({
        "title": "Test message",
        "detail_html": args.text,
        "tool": "test",
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
