#!/usr/bin/env python3
"""Stop the Imp render server.

Inputs:
  --port (int, optional): Port the server runs on (default: 8421).

Process: Finds the process listening on the port and sends SIGTERM.
Falls back to SIGKILL if the process doesn't exit within 5 seconds.

Output: Prints whether the server was stopped or wasn't running."""

import argparse
import signal
import subprocess
import sys
import time


def find_pids(port):
    """Find PIDs listening on the given port."""
    try:
        result = subprocess.run(
            ["lsof", "-i", f":{port}", "-t"],
            capture_output=True, text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return [int(p) for p in result.stdout.strip().split("\n")]
    except Exception:
        pass
    return []


def main() -> int:
    parser = argparse.ArgumentParser(description="Stop the Imp server")
    parser.add_argument("--port", type=int, default=8421)
    args = parser.parse_args()

    pids = find_pids(args.port)
    if not pids:
        print(f"No server running on port {args.port}")
        return 0

    print(f"Stopping server (PIDs: {pids})...")
    import os
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    # Wait for clean shutdown
    for _ in range(5):
        time.sleep(1)
        if not find_pids(args.port):
            print("Server stopped")
            return 0

    # Force kill
    print("Force killing...")
    for pid in find_pids(args.port):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    print("Server stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
