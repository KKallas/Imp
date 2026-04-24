#!/usr/bin/env python3
"""Check the Imp server is running and return its URL.

Inputs:
  --port (int, optional): Port to check (default: 8421).

Process: Checks the health endpoint. If responding, returns the server URL.
If not, reports the error.

Output: Prints the server URL or error details."""

import argparse
import socket
import sys
import urllib.request


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
    parser = argparse.ArgumentParser(description="Check the Imp server")
    parser.add_argument("--port", type=int, default=8421)
    args = parser.parse_args()

    ip = get_lan_ip()
    url = f"http://{ip}:{args.port}"

    try:
        resp = urllib.request.urlopen(f"http://127.0.0.1:{args.port}/health", timeout=3)
        print(f"Server running at {url}")
        print(f"Sync download: {url}/imp-sync.py")
        return 0
    except urllib.error.URLError as e:
        print(f"Server not responding on port {args.port}")
        print(f"Error: {e.reason}")
        print(f"Start with: python -m server.render_route --port {args.port}")
        return 1
    except Exception as e:
        print(f"Server not responding on port {args.port}")
        print(f"Error: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
