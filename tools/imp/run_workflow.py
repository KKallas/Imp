#!/usr/bin/env python3
"""Run a workflow by name via the Imp API.

Inputs:
  name (positional): Workflow name to run.
  --port: int — server port (default: 8421).
  --wait: Wait for the workflow to finish and print results.

Process: Calls POST /api/workflows/<name>/start on the local server,
         optionally polls until completion.
Output: Prints workflow status and step results."""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request


def api(method, url):
    req = urllib.request.Request(url, method=method)
    if method == "POST":
        req.add_header("Content-Type", "application/json")
        req.data = b"{}"
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode()}", file=sys.stderr)
        return None
    except urllib.error.URLError as e:
        print(f"Connection error: {e.reason}", file=sys.stderr)
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a workflow")
    parser.add_argument("name", help="Workflow name")
    parser.add_argument("--port", type=int, default=8421)
    parser.add_argument("--wait", action="store_true", help="Wait for completion")
    args = parser.parse_args()

    base = f"http://127.0.0.1:{args.port}"

    result = api("POST", f"{base}/api/workflows/{args.name}/start")
    if result is None:
        return 1

    print(f"Started: {args.name} (status: {result.get('status', '?')})")

    if not args.wait:
        return 0

    while True:
        time.sleep(1)
        status = api("GET", f"{base}/api/workflows/{args.name}")
        if status is None:
            return 1
        s = status.get("status", "unknown")
        step = status.get("current_step", "?")
        print(f"  status: {s}, step: {step}", end="\r")
        if s in ("done", "error", "idle"):
            print()
            break

    # Print step results
    detail = api("GET", f"{base}/api/workflows/{args.name}")
    if detail:
        for step in detail.get("steps", []):
            icon = "ok" if step.get("result", {}).get("ok") else "err"
            name = step.get("description") or step.get("name", "?")
            print(f"  [{icon}] {name}")
            output = step.get("result", {}).get("output", "")
            if output:
                for line in output.split("\n")[:5]:
                    print(f"        {line}")
        print(f"\nWorkflow {args.name}: {detail.get('status', '?')}")

    return 0 if detail and detail.get("status") == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
