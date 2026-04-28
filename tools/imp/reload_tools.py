#!/usr/bin/env python3
"""Reload the cached tool and workflow list.

Inputs: None.

Process: Calls the server's /api/reload-prompt endpoint to force the
         Foreman agent to re-scan tools/ and workflows/ directories.
Output: Prints confirmation."""
import json
import sys
import urllib.error
import urllib.request


def main() -> int:
    url = "http://127.0.0.1:8421/api/reload-prompt"
    try:
        req = urllib.request.Request(url, method="POST", data=b"{}")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            print(f"Reloaded. Prompt length: {data.get('length', '?')} chars.")
            return 0
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode()}", file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"Server not reachable: {e.reason}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
