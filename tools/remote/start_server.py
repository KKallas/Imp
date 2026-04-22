#!/usr/bin/env python3
"""Start the Imp render server on port 8421.

Inputs:
  --port (int, optional): Port to run on (default: 8421).

Process: Checks if the server is already running. If not, spawns it as a
background process. Waits for the health endpoint to respond.

Output: Prints the server URL or reports that it's already running."""

import argparse
import socket
import subprocess
import sys
import time
import urllib.request


def is_running(port):
    """Check if the server is already responding."""
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2)
        return True
    except Exception:
        return False


def get_lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def main() -> int:
    parser = argparse.ArgumentParser(description="Start the Imp server")
    parser.add_argument("--port", type=int, default=8421)
    args = parser.parse_args()

    if is_running(args.port):
        ip = get_lan_ip()
        print(f"Server already running at http://{ip}:{args.port}")
        return 0

    print(f"Starting server on port {args.port}...")
    subprocess.Popen(
        [sys.executable, "-m", "server.render_route", "--port", str(args.port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    # Wait for it to come up
    for _ in range(10):
        time.sleep(1)
        if is_running(args.port):
            ip = get_lan_ip()
            print(f"Server started at http://{ip}:{args.port}")
            return 0

    print("Server failed to start within 10 seconds")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
