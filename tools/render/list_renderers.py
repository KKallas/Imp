#!/usr/bin/env python3
"""List all available renderers on the Imp server.

Inputs:
  --port: int — server port (default: 8421).

Process: Calls the /health endpoint to get the list of registered renderers.
Output: Prints available renderer names."""
import json
import sys
import urllib.error
import urllib.request


def main() -> int:
    port = 8421
    if "--port" in sys.argv:
        idx = sys.argv.index("--port")
        port = int(sys.argv[idx + 1])

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as resp:
            data = json.loads(resp.read().decode())
            renderers = data.get("renderers", [])
            print(f"{len(renderers)} renderer(s) available:\n")
            for r in renderers:
                print(f"  {r}")
            return 0
    except Exception as e:
        print(f"Could not reach server: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
