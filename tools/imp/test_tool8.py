#!/usr/bin/env python3
"""Post a test message to the Imp queue.

Inputs:
  --message: str — the text content of the queue message (required).

Process: Sends a POST request to the local Imp queue API with the
         provided message as the detail text.
Output: Prints the created queue item JSON."""
import argparse
import json
import sys
import urllib.error
import urllib.request


def main() -> int:
    parser = argparse.ArgumentParser(description="Post a test message to the queue.")
    parser.add_argument("--message", required=True, help="Text content of the queue message.")
    args = parser.parse_args()

    url = "http://127.0.0.1:8421/api/queue"
    payload = json.dumps({
        "title": "Test Message",
        "detail_html": args.message,
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
